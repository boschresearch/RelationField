# Copyright (c) 2025 Robert Bosch GmbH
# SPDX-License-Identifier: AGPL-3.0

# This source code is derived from GARField
#   (https://github.com/chungmin99/garfield
# Copyright (c) 2014 GARField authors, licensed under the MIT license,
# cf. 3rd-party-licenses.txt file in the root directory of this source tree.

import typing
from dataclasses import dataclass, field
from typing import Literal, Type, Mapping, Any

import torch
from nerfstudio.pipelines.base_pipeline import VanillaPipeline, VanillaPipelineConfig
from torch.cuda.amp.grad_scaler import GradScaler

import tqdm

from sklearn.preprocessing import QuantileTransformer
from relationfield.relationfield_datamanager import RelationFieldDataManagerConfig, RelationFieldDataManager
from relationfield.relationfield_model import RelationFieldModel, RelationFieldModelConfig


@dataclass
class RelationFieldPipelineConfig(VanillaPipelineConfig):
    """Configuration for relationfield pipeline instantiation"""

    _target: Type = field(default_factory=lambda: RelationFieldPipeline)
    """target class to instantiate"""
    datamanager: RelationFieldDataManagerConfig = RelationFieldDataManagerConfig()
    model: RelationFieldModelConfig = RelationFieldModelConfig()

    start_grouping_step: int = 1000
    max_grouping_scale: float = 2.0
    num_rays_per_image: int = 256
    normalize_grouping_scale: bool = True


class RelationFieldPipeline(VanillaPipeline):
    config: RelationFieldPipelineConfig
    datamanager: RelationFieldDataManager
    model: RelationFieldModel

    def __init__(
        self,
        config: RelationFieldPipelineConfig,
        device: str,
        test_mode: Literal["test", "val", "inference"] = "val",
        world_size: int = 1,
        local_rank: int = 0,
        grad_scaler: typing.Optional[GradScaler] = None,
    ):
        config.model.max_grouping_scale = config.max_grouping_scale
        super().__init__(
            config,
            device,
            test_mode,
            world_size,
            local_rank,
            grad_scaler,
        )

    @torch.autocast('cuda')
    def get_train_loss_dict(self, step: int):
        """In addition to the base class, we also calculate SAM masks
        and their 3D scales at `start_grouping_step`."""
        if step == self.config.start_grouping_step:
            loaded = self.datamanager.load_sam_data()
            if not loaded:
                self.populate_grouping_info()
            else:
                # Initialize grouping statistics. This will be automatically loaded from a checkpoint next time.
                scale_stats = self.datamanager.scale_3d_statistics
                self.grouping_stats = torch.nn.Parameter(scale_stats)
                self.model.grouping_field.quantile_transformer = (
                    self._get_quantile_func(scale_stats)
                )
            # Set the number of rays per image to the number of rays per image for grouping
            pixel_sampler = self.datamanager.train_pixel_sampler
            pixel_sampler.num_rays_per_image = pixel_sampler.config.num_rays_per_image

        ray_bundle, batch = self.datamanager.next_train(step)
        if step >= self.config.start_grouping_step:
            # also set the grouping info in the batch; in-place operation
            self.datamanager.next_group(ray_bundle, batch)
            self.datamanager.next_rel_map(ray_bundle, batch)

        model_outputs = self._model(
            ray_bundle, batch
        )  # train distributed data parallel model if world_size > 1

        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(model_outputs, batch, metrics_dict)
        
        if step >= self.config.start_grouping_step:
            loss_dict.update(
                self.model.get_loss_dict_group(model_outputs, batch, metrics_dict)
            )
            
            loss_dict.update(self.model.get_loss_dict_segmentation(model_outputs, batch, metrics_dict))
            if step >= self.config.start_grouping_step+0:    
                loss_dict.update(
                    self.model.get_loss_dict_relation(model_outputs, batch, metrics_dict)
                )   

        return model_outputs, loss_dict, metrics_dict

    def populate_grouping_info(self):
        """
        Calculate groups from SAM and their 3D scales, and save them in the datamanager.
        This information is required to supervise the grouping field.
        """
        # Note that pipeline is in train mode here, via the base trainer.
        self.model.eval()

        # Calculate multi-scale masks, and their 3D scales
        scales_3d_list, pixel_level_keys_list, group_cdf_list = [], [], []
        train_cameras = self.datamanager.train_dataset.cameras
        for i in tqdm.trange(len(train_cameras), desc="Calculating 3D masks"):
            camera_ray_bundle = train_cameras.generate_rays(camera_indices=i).to(
                self.device
            )
            with torch.no_grad():
                outputs = self.model.get_outputs_for_camera_ray_bundle(
                    camera_ray_bundle, #batch={'pcd':self.datamanager.pcd}
                )

            # Get RGB (for SAM mask generation), depth and 3D point locations (for 3D scale calculation)
            rgb = self.datamanager.train_dataset[i]["image"]
            if "depth_image" in self.datamanager.train_dataset[i]:
                depth = self.datamanager.train_dataset[i]["depth_image"].to(self.device)
            else:
                depth = outputs["depth"]
            points = camera_ray_bundle.origins + camera_ray_bundle.directions * depth
            # Scales are capped to `max_grouping_scale` to filter noisy / outlier masks.
            (
                pixel_level_keys,
                scale_3d,
                group_cdf,
            ) = self.datamanager._calculate_3d_groups(
                rgb, depth, points, max_scale=self.config.max_grouping_scale
            )

            pixel_level_keys_list.append(pixel_level_keys)
            scales_3d_list.append(scale_3d)
            group_cdf_list.append(group_cdf)

        # Save grouping data, and set it in the datamanager for current training.
        # This will be cached, so we don't need to calculate it again.
        self.datamanager.save_sam_data(
            pixel_level_keys_list, scales_3d_list, group_cdf_list
        )
        self.datamanager.pixel_level_keys = torch.nested.nested_tensor(
            pixel_level_keys_list
        )
        self.datamanager.scale_3d = torch.nested.nested_tensor(scales_3d_list)
        self.datamanager.group_cdf = torch.nested.nested_tensor(group_cdf_list)

        # Initialize grouping statistics. This will be automatically loaded from a checkpoint next time.
        self.grouping_stats = torch.nn.Parameter(torch.cat(scales_3d_list))
        self.model.grouping_field.quantile_transformer = self._get_quantile_func(
            torch.cat(scales_3d_list)
        )

        # Turn model back to train mode
        self.model.train()

    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = False):
        """
        Same as the base class, but also loads the grouping statistics.
        It's important to normalize the 3D scales as input to the grouping field.
        """
        # Load 3D group scale statistics
        grouping_stats = state_dict["grouping_stats"]
        self.grouping_stats = torch.nn.Parameter(torch.zeros_like(grouping_stats)).to(
            self.device
        )
        # Calculate quantile transformer
        self.model.grouping_field.quantile_transformer = self._get_quantile_func(
            grouping_stats
        )

        return super().load_state_dict(state_dict, strict)

    def _get_quantile_func(self, scales: torch.Tensor, distribution="normal"):
        """
        Use 3D scale statistics to normalize scales -- use quantile transformer.
        """
        scales = scales.flatten()
        scales = scales[(scales > 0) & (scales < self.config.max_grouping_scale)]

        scales = scales.detach().cpu().numpy()

        # Calculate quantile transformer
        quantile_transformer = QuantileTransformer(output_distribution=distribution)
        quantile_transformer = quantile_transformer.fit(scales.reshape(-1, 1))

        def quantile_transformer_func(scales):
            # This function acts as a wrapper for QuantileTransformer.
            # QuantileTransformer expects a numpy array, while we have a torch tensor.
            return torch.Tensor(
                quantile_transformer.transform(scales.cpu().numpy())
            ).to(scales.device)

        return quantile_transformer_func

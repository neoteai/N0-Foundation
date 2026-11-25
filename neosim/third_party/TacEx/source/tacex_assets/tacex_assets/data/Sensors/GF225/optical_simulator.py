"""
Optical simulator for vitai sensor simulator.
Generate tactile rgb image using sensor camera depth info.
This file implements the optical simulator using mlp rendering.
Strict image resolution is 480 x 480 for gf225 sensor.
For other tactile resolutions lower than that, downsampling is applied.
"""

from __future__ import annotations

import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import TYPE_CHECKING

import cv2
import omni.ui

from vitai_core.utils.logger import get_logger



if TYPE_CHECKING:
    from .optical_simulator_cfg import OpticalSimulatorCfg
    from vitai_core.sensors.gf225.gf225 import GF225Sensor



class MLP(nn.Module):
    """Multi-layer perceptron for normal-to-color mapping in optical simulation."""
    
    dropout_p = 0.05
    def __init__(self, input_size = 5, output_size = 3, hidden_size = 32):
        super().__init__()
        self.fc1 = nn.Linear(input_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)
        self.fc3 = nn.Linear(hidden_size, hidden_size)
        self.fc4 = nn.Linear(hidden_size, output_size)
        self.drop = nn.Dropout(p=self.dropout_p)
    
    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = self.drop(x)
        x = F.relu(self.fc2(x))
        x = self.drop(x)
        x = self.fc3(x)
        x = self.drop(x)
        x = self.fc4(x)
        return x

class OpticalSimulator:
    """Optical simulation approach for GF225 sensor via camera depth data.
    
    Uses MLP-based rendering to convert depth maps to tactile RGB images.
    """
    cfg: OpticalSimulatorCfg

    def __init__(self, cfg: OpticalSimulatorCfg, sensor: "GF225Sensor"):
        """Initialize the optical simulator.
        
        Args:
            cfg: Configuration for optical simulator
            sensor: Parent GF225 sensor instance
        """
        self.cfg = cfg
        self.sensor = sensor
        self._logger = get_logger("vitai.optical_sim", prim=str(sensor.cfg.prim_path))

    def _initialize_impl(self):
        """Initialize optical simulator assets and models."""
        if self.cfg.device is None:
            self._device = self.sensor.device
        else:
            self._device = self.cfg.device
        
        self._num_envs = self.sensor._num_envs

        # Load optical sim assets
        assets_dir = os.path.join(os.path.dirname(__file__), "optical_sim_utils")
        self.bg_img = np.load(os.path.join(assets_dir, "vitai_bg.npy"))
        self.bg_render = np.load(os.path.join(assets_dir, "init_bg_fots_vitai.npy"))
        self.bg_depth = np.load(os.path.join(assets_dir, "ini_depth_sim_d0.npy"))

        # Load optical MLP model
        self.mlp = MLP().to(self._device)
        weights_path = os.path.join(assets_dir, "mlp_n2c_vitai5.pth")
        self.mlp.load_state_dict(torch.load(weights_path, map_location=self._device))
        self.mlp.eval()
        
        self._logger.debug(
            f"Optical simulator initialized: device={self._device}, "
            f"num_envs={self._num_envs}, "
            f"tactile_res={self.cfg.tactile_img_res}"
        )

    
    # Helper method for optical simulation
    def _padding(self,img):
        if len(img.shape)==2:
            return np.pad(img, ((1,1), (1,1)), "symmetric")
        elif len(img.shape) == 3:
            return np.pad(img, ((1,1), (1,1), (0,0)), "symmetric")
        return img
    
    def _generate_normals(self, height_map):
        """Generate surface normals from height map.
        
        Args:
            height_map: 2D array of height values
            
        Returns:
            Surface normals as (H, W, 3) array
        """
        h, w = height_map.shape
        top = height_map[0:h-2, 1:w-1]
        bot = height_map[2:h, 1:w-1]
        left = height_map[1:h-1, 0:w-2]
        right = height_map[1:h-1, 2:w]

        dzdx = (bot - top) / 2.0
        dzdy = (right - left) / 2.0

        direction = np.ones((h - 2, w - 2, 3))
        direction[:, :, 0] = -dzdy
        direction[:, :, 1] = dzdx

        magnitude = np.sqrt(direction[:, :, 0] ** 2 + direction[:, :, 1] ** 2 + direction[:, :, 2] ** 2)
        normal = direction / magnitude[:, :, np.newaxis]

        normal = self._padding(normal)
        normal = (normal + 1.0) * 0.5
        return normal

    
    def _smooth_height_map(self, height_map):
        """Smooth height map using multi-scale Gaussian blurring.
        
        Args:
            height_map: Raw depth/height map from camera
            
        Returns:
            Smoothed height map
        """
        diff_depth = np.abs(height_map - self.bg_depth)
        diff_depth[np.where(abs(diff_depth) < 6e-5)] = 0.0

        diff_depth *= 1000.0
        diff_depth /= (0.05107 * 2)

        contact_mask = diff_depth > (np.max(diff_depth) * 0.4)
        height_map = diff_depth
        zq_back = height_map.copy()

        kernel_sizes = [101, 51, 21, 11, 5]
        for k in kernel_sizes:
            height_map = cv2.GaussianBlur(height_map.astype(np.float32), (k, k), 0)
            height_map[contact_mask] = zq_back[contact_mask]

        height_map = cv2.GaussianBlur(height_map.astype(np.float32), (5, 5), 0)
        return height_map
    
    def _preproc_mlp(self, normal):
        """Preprocess normals for MLP input.
        
        Args:
            normal: Surface normal map (H, W, 3)
            
        Returns:
            Torch tensor ready for MLP input (N, 5) where N = H * W
        """
        h, w = normal.shape[:2]

        rows, cols = np.indices((h, w))

        n_pixels = h * w
        xy_coords = np.stack([rows.ravel(), cols.ravel()], axis=1)
        nxyz = normal.reshape(n_pixels, 3)
        
        # Normalize coordinates based on actual image size
        norm_x = xy_coords[:, 0] / float(h)
        norm_y = xy_coords[:, 1] / float(w)

        input_data = np.column_stack((norm_x, norm_y, nxyz))
        return torch.tensor(input_data, dtype=torch.float32, device=self._device)

    # excute in main loop and output marker image
    def optical_simulation(self):
        """Simulate tactile RGB image from depth data.
        
        Returns:
            Torch tensor of shape (num_envs, H, W, 3) containing RGB images
        """
        if "camera_depth" not in self.sensor._data.output:
            self._logger.warning("No camera depth data found, returning zero image")
            return torch.zeros(
                (self._num_envs, self.cfg.tactile_img_res[1], self.cfg.tactile_img_res[0], 3),
                device=self._device,
                dtype=torch.uint8,
            )

        # Process each environment
        # For now, process first environment (TODO: extend to multi-env)
        depth_tensor = self.sensor._data.output["camera_depth"][0]
        if depth_tensor.dim() == 3:
            depth_tensor = depth_tensor.squeeze(-1)

        height_map = depth_tensor.cpu().numpy()
        
        # Processing pipeline
        height_map = self._smooth_height_map(height_map)
        normal = self._generate_normals(height_map)
        img_n = self._preproc_mlp(normal)

        # MLP inference
        with torch.no_grad():
            sim_img_r = self.mlp(img_n)

        # Post-process
        sim_img_r = sim_img_r.cpu().numpy()
        out_diff = (sim_img_r * 2 - 1) * 255
        h, w = height_map.shape
        sim_img = out_diff.reshape(h, w, 3).astype(np.float32)
        sim_img = sim_img + self.bg_img
        sim_img = np.clip(sim_img, 0, 255).astype(np.uint8)

        return torch.tensor(sim_img, device=self._device).unsqueeze(0)

    
    def compute_indentation_depth(self):
        """Compute indentation depth from optical simulation.
        
        TODO: Implement indentation depth calculation
        """
        return torch.zeros(self._num_envs, device=self._device)
    
    def reset(self):
        pass

    def _set_debug_vis_impl(self, debug_vis: bool):
        """Setup debug visualization for optical simulation."""
        if debug_vis and not hasattr(self, "_debug_windows"):
            self._debug_windows = {}
            self._debug_img_providers = {}
    
    def _debug_vis_callback(self, event):
        """Debug visualization callback for showing tactile RGB output."""
        if self.sensor._prim_view is None:
            return

        for i, prim in enumerate(self.sensor._prim_view.prims):
            if "tactile_rgb" not in self.sensor.cfg.data_types:
                continue

            attr = prim.GetAttribute("debug_tactile_rgb")
            if not attr.IsValid():
                continue

            show_img = attr.Get()
            if show_img:
                if str(i) not in self._debug_windows:
                    self._debug_windows[str(i)] = omni.ui.Window(
                        f"{self.sensor._prim_view.prim_paths[i]}/optical_sim",
                        width=self.cfg.tactile_img_res[0],
                        height=self.cfg.tactile_img_res[1],
                    )
                    self._debug_img_providers[str(i)] = omni.ui.ByteImageProvider()

                # Get image data
                img_data = self.sensor.data.output["tactile_rgb"][i]

                if isinstance(img_data, torch.Tensor):
                    img_np = img_data.cpu().numpy()
                else:
                    img_np = img_data

                # Convert to RGBA
                if img_np.shape[-1] == 3:
                    alpha = np.full((img_np.shape[0], img_np.shape[1], 1), 255, dtype=img_np.dtype)
                    img_np = np.concatenate([img_np, alpha], axis=-1)

                # Update provider
                self._debug_img_providers[str(i)].set_bytes_data(
                    img_np.flatten().tolist(),
                    [img_np.shape[1], img_np.shape[0]]
                )

                with self._debug_windows[str(i)].frame:
                    omni.ui.ImageWithProvider(self._debug_img_providers[str(i)])

            elif str(i) in self._debug_windows:
                self._debug_windows[str(i)].visible = False
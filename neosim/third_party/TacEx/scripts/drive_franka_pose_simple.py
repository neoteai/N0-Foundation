"""
TacEx Simple Franka Pose Controller

Features:
    - Given a target pose (position + quaternion), move the Franka end-effector to it
    - Uses TacEx's built-in DifferentialIK controller

Examples:
    python drive_franka_pose_simple.py --target_pos 0.5 0.2 0.3 --target_quat 0 0 0 1
    python drive_franka_pose_simple.py --target_pos 0.4 0.1 0.4 --target_quat 0.707 0 0 0.707
"""

from __future__ import annotations

import argparse
from isaaclab.app import AppLauncher

# Parse command-line arguments
parser = argparse.ArgumentParser(
    description="Control Franka arm to reach target pose using minimal implementation"
)
parser.add_argument("--target_pos", nargs=3, type=float, default=[0.5, 0.2, 0.3], 
                   help="Target position [x, y, z]")
parser.add_argument("--target_quat", nargs=4, type=float, default=[0, 0, 0, 1], 
                   help="Target quaternion [qx, qy, qz, qw]")
parser.add_argument("--hold_time", type=float, default=3.0, 
                   help="Time to hold position after reaching target (seconds)")
AppLauncher.add_app_launcher_args(parser)

args_cli = parser.parse_args()
# Enable rendering options
args_cli.enable_cameras = True
args_cli.headless = False

# Launch Isaac Sim
app_launcher = AppLauncher(args_cli)
simulation_app = app_launcher.app

import numpy as np
import torch
import traceback
import carb

import isaaclab.sim as sim_utils
import isaaclab.utils.math as math_utils
from isaaclab.assets import Articulation, ArticulationCfg
from isaaclab.controllers.differential_ik import DifferentialIKController
from isaaclab.controllers.differential_ik_cfg import DifferentialIKControllerCfg
from isaaclab.scene import InteractiveScene, InteractiveSceneCfg
from isaaclab.sim import SimulationContext, SimulationCfg
from isaaclab.utils import configclass

from tacex_assets.robots.franka.franka_gsmini_single_rigid import (
    FRANKA_PANDA_ARM_SINGLE_GSMINI_HIGH_PD_RIGID_CFG,
)


@configclass
class PoseControllerCfg:
    """Minimal pose controller configuration."""
    
    # Simulation settings
    sim_dt: float = 1.0 / 60.0  # 60Hz
    decimation: int = 1         # control frequency multiplier
    
    # Scene settings
    num_envs: int = 1
    env_spacing: float = 2.0
    
    # IK controller settings
    ik_controller_cfg = DifferentialIKControllerCfg(
        command_type="pose", 
        use_relative_mode=False, 
        ik_method="svd"
    )
    
    # Tolerance settings
    position_tolerance: float = 0.002   # 2mm
    rotation_tolerance: float = 0.02    # ~1.1 deg


class MinimalPoseController:
    """Minimal Franka pose controller."""
    
    def __init__(self, cfg: PoseControllerCfg):
        self.cfg = cfg
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        
        # Initialize simulation
        self._setup_simulation()
        
        # Build the scene
        self._setup_scene() 
        
        # Initialize the robot (basic part; does not touch PhysX properties)
        self._setup_robot_basic()
        
        # State variables
        self.target_pose = torch.zeros((self.cfg.num_envs, 7), device=self.device)
        self.step_count = 0
        self._robot_initialized = False
        
        print("[INFO] Pose controller initialized")
    
    def _setup_simulation(self):
        """Set up the simulation."""
        sim_cfg = SimulationCfg(
            dt=self.cfg.sim_dt,
            render_interval=self.cfg.decimation
        )
        
        # Create the simulation context
        self.sim = SimulationContext(sim_cfg)
        
        # Select device
        if "cuda" in self.device:
            torch.cuda.set_device(self.device)
            
        print(f"[INFO] Simulation initialized")
    
    def _setup_scene(self):
        """Set up the scene."""
        scene_cfg = InteractiveSceneCfg(
            num_envs=self.cfg.num_envs,
            env_spacing=self.cfg.env_spacing,
            replicate_physics=True
        )
        
        self.scene = InteractiveScene(scene_cfg)
        
        # Ground plane
        ground_cfg = sim_utils.GroundPlaneCfg()
        ground_cfg.func("/World/ground", ground_cfg)
        
        # Lighting
        light_cfg = sim_utils.DomeLightCfg(color=(0.75, 0.75, 0.75), intensity=3000.0)
        light_cfg.func("/World/light", light_cfg)
        
        print("[INFO] Scene created")
    
    def _setup_robot_basic(self):
        """Set up the robot (basic part)."""
        robot_cfg = FRANKA_PANDA_ARM_SINGLE_GSMINI_HIGH_PD_RIGID_CFG.replace(
            prim_path="/World/envs/env_.*/Robot"
        )
        
        self._robot = Articulation(robot_cfg)
        self.scene.articulations["robot"] = self._robot
        self.scene.clone_environments(copy_from_source=False)
        
        # End-effector offset
        self._offset_pos = torch.tensor([0.0, 0.0, 0.131], device=self.device).repeat(self.cfg.num_envs, 1)
        self._offset_rot = torch.tensor([1.0, 0.0, 0.0, 0.0], device=self.device).repeat(self.cfg.num_envs, 1)
        
        print("[INFO] Robot created")
    
    def _setup_robot_properties(self):
        """Set up robot properties."""
        body_ids, body_names = self._robot.find_bodies("panda_hand")
        self._body_idx = body_ids[0]
        self._body_name = body_names[0]
        self._jacobi_body_idx = self._body_idx - 1
    
    def _setup_ik_controller(self):
        """Set up the IK controller."""
        self._ik_controller = DifferentialIKController(
            cfg=self.cfg.ik_controller_cfg, 
            num_envs=self.cfg.num_envs, 
            device=self.device
        )
        
        self.ik_commands = torch.zeros((self.cfg.num_envs, 7), device=self.device)
        print("[INFO] IK controller initialized")
    
    def start_simulation(self):
        """Start the simulation."""
        print("[INFO] Starting simulation...")
        self.sim.reset()
        self.scene.update(dt=self.cfg.sim_dt)
        
        self._setup_robot_properties()
        self._setup_ik_controller()
        
        self._robot_initialized = True
        print("[INFO] Simulation ready")
    
    def reset(self):
        """Reset the environment."""
        joint_pos = self._robot.data.default_joint_pos.clone()
        joint_vel = torch.zeros_like(joint_pos)
        
        self._robot.set_joint_position_target(joint_pos)
        self._robot.write_joint_state_to_sim(joint_pos, joint_vel)
        
        self.step_count = 0
    
    def set_target_pose(self, target_pos: list, target_quat: list, env_idx: int = 0):
        """Set the target pose."""
        self.target_pose[env_idx, :3] = torch.tensor(target_pos, device=self.device)
        self.target_pose[env_idx, 3:] = torch.tensor(target_quat, device=self.device)
        
        self.ik_commands[env_idx, :3] = torch.tensor(target_pos, device=self.device)
        self.ik_commands[env_idx, 3:] = torch.tensor(target_quat, device=self.device)
        
        print(f"[INFO] Target pose set: pos={target_pos}, quat={target_quat}")
    
    def step(self):
        """Run one simulation step."""
        if not self._robot_initialized:
            raise RuntimeError("Robot not initialized")
            
        self._ik_controller.set_command(self.ik_commands)
        self._apply_ik_control()
        
        self.scene.write_data_to_sim()
        self.sim.step(render=True)
        simulation_app.update()
        self.scene.update(dt=self.cfg.sim_dt)
        
        self.step_count += 1
    
    def _apply_ik_control(self):
        """Apply IK control."""
        ee_pos_curr, ee_quat_curr = self._compute_frame_pose()
        joint_pos = self._robot.data.joint_pos[:, :]
        
        if ee_pos_curr.norm() != 0:
            jacobian = self._compute_frame_jacobian()
            joint_pos_des = self._ik_controller.compute(ee_pos_curr, ee_quat_curr, jacobian, joint_pos)
        else:
            joint_pos_des = joint_pos.clone()
            
        self._robot.set_joint_position_target(joint_pos_des)

    def check_pose_reached(self, env_idx: int = 0) -> tuple:
        """Check whether the target pose has been reached."""
        if not self._robot_initialized:
            return False, float('inf'), float('inf')
            
        ee_pos_curr, ee_quat_curr = self._compute_frame_pose()
        
        pos_error = torch.norm(ee_pos_curr[env_idx] - self.target_pose[env_idx, :3]).item()
        quat_dot = torch.sum(ee_quat_curr[env_idx] * self.target_pose[env_idx, 3:]).item()
        rot_error = 1.0 - abs(quat_dot)
        
        pos_ok = pos_error < self.cfg.position_tolerance
        rot_ok = rot_error < self.cfg.rotation_tolerance
        
        return pos_ok and rot_ok, pos_error, rot_error
    
    def get_current_pose(self, env_idx: int = 0) -> tuple:
        """Get the current end-effector pose."""
        if not self._robot_initialized:
            return np.zeros(3), np.array([0, 0, 0, 1])
            
        ee_pos, ee_quat = self._compute_frame_pose()
        return ee_pos[env_idx].cpu().numpy(), ee_quat[env_idx].cpu().numpy()
    
    def close(self):
        """Close the controller."""
        pass
    
    
    # IK computation helpers
    @property
    def jacobian_w(self) -> torch.Tensor:
        """Jacobian in the world frame."""
        return self._robot.root_physx_view.get_jacobians()[:, self._jacobi_body_idx, :, :]

    @property
    def jacobian_b(self) -> torch.Tensor:
        """Jacobian in the base frame."""
        jacobian = self.jacobian_w
        base_rot = self._robot.data.root_link_quat_w
        base_rot_matrix = math_utils.matrix_from_quat(math_utils.quat_inv(base_rot))
        jacobian[:, :3, :] = torch.bmm(base_rot_matrix, jacobian[:, :3, :])
        jacobian[:, 3:, :] = torch.bmm(base_rot_matrix, jacobian[:, 3:, :])
        return jacobian

    def _compute_frame_pose(self) -> tuple[torch.Tensor, torch.Tensor]:
        """Compute the target frame pose in the root frame.

        Returns:
            (position, quaternion) tuple
        """
        # Read simulation data
        ee_pos_w = self._robot.data.body_link_pos_w[:, self._body_idx]
        ee_quat_w = self._robot.data.body_link_quat_w[:, self._body_idx]
        root_pos_w = self._robot.data.root_link_pos_w
        root_quat_w = self._robot.data.root_link_quat_w
        
        # Compute relative pose
        ee_pose_b, ee_quat_b = math_utils.subtract_frame_transforms(root_pos_w, root_quat_w, ee_pos_w, ee_quat_w)
        
        # Apply the offset
        ee_pose_b, ee_quat_b = math_utils.combine_frame_transforms(
            ee_pose_b, ee_quat_b, self._offset_pos, self._offset_rot
        )

        return ee_pose_b, ee_quat_b

    def _compute_frame_jacobian(self) -> torch.Tensor:
        """Compute the geometric Jacobian of the target frame in the root frame."""
        # Read the parent body's Jacobian
        jacobian = self.jacobian_b

        # Account for the offset
        # translational part: v_link = v_ee + w_ee x r_link_ee
        jacobian[:, 0:3, :] += torch.bmm(-math_utils.skew_symmetric_matrix(self._offset_pos), jacobian[:, 3:, :])
        
        # rotational part: w_link = R_link_ee @ w_ee  
        jacobian[:, 3:, :] = torch.bmm(math_utils.matrix_from_quat(self._offset_rot), jacobian[:, 3:, :])

        return jacobian


def main():
    """Main entry point."""
    print(f"[INFO] Starting Franka pose controller")
    print(f"[INFO] Target position: {args_cli.target_pos}")
    print(f"[INFO] Target quaternion: {args_cli.target_quat}")
    
    target_pos = np.array(args_cli.target_pos)
    target_quat = np.array(args_cli.target_quat)
    
    # Validate the quaternion
    quat_norm = np.linalg.norm(target_quat)
    if quat_norm < 1e-6:
        print(f"[ERROR] Invalid quaternion: {target_quat}")
        return
    
    # Normalize the quaternion
    target_quat = target_quat / quat_norm
    args_cli.target_pos = target_pos.tolist()
    args_cli.target_quat = target_quat.tolist()
    
    try:
        cfg = PoseControllerCfg()
        controller = MinimalPoseController(cfg)
        
        controller.start_simulation()
        controller.reset()
        controller.set_target_pose(args_cli.target_pos, args_cli.target_quat)
        
        max_steps = 1200  # 20 seconds
        print("[INFO] Moving to target pose...")
        
        reached = False
        for step in range(max_steps):
            controller.step()
            
            # Check progress once per second
            if step % 60 == 0:
                reached, pos_error, rot_error = controller.check_pose_reached()
                if reached:
                    print(f"[SUCCESS] Step {step}: target pose reached!")
                    break
                else:
                    print(f"[INFO] Step {step}: position error {pos_error:.4f}m, rotation error {rot_error:.4f}")
        
        if not reached:
            print("[INFO] Target pose not fully reached")
        
        # Hold position
        hold_steps = int(args_cli.hold_time * 60)
        print(f"[INFO] Holding position for {args_cli.hold_time} s...")
        for _ in range(hold_steps):
            controller.step()
        
        # Report the final pose
        final_pos, final_quat = controller.get_current_pose()
        print(f"[INFO] Final position: {final_pos}")
        print(f"[INFO] Final quaternion: {final_quat}")
        
        print("[INFO] Task complete")
        controller.close()
        
    except Exception as err:
        carb.log_error(f"Execution error: {err}")
        carb.log_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as err:
        carb.log_error(err)
        carb.log_error(traceback.format_exc())
        raise
    finally:
        # Close the simulation app
        simulation_app.close()
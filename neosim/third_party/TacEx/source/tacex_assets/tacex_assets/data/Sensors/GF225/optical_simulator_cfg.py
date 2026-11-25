"""Configuration for optical simulation via camera depth data."""

from isaaclab.utils import configclass


@configclass
class OpticalSimulatorCfg:
    """Configuration for optical simulator."""

    device: str | None = None
    """Device for computation. If None, uses sensor's device."""

    tactile_img_res: tuple[int, int] = (480, 480)
    """Resolution of the tactile image output (width, height).
    # TODO change to inherit from GF225SensorCfg defined resolution
    
    Can be different from the sensor camera resolution.
    If different, height map from camera will be up/downsampled.
    """
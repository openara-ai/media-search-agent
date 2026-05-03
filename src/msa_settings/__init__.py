"""Package entry for msa_settings.

Provides load_config() which returns structured Config dataclass.
"""
from .config import load_config, detect_device
from .instance_lock import acquire_instance_lock, release_instance_lock

__all__ = ["load_config", "detect_device", "acquire_instance_lock", "release_instance_lock"]
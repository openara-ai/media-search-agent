"""Shared types for face detection backends."""
from dataclasses import dataclass
from typing import Any, Dict, Tuple

import numpy as np


@dataclass
class FaceDetection:
    """Single face detection result."""
    bbox: Tuple[float, float, float, float]  # (x, y, w, h) normalised 0-1
    embedding: np.ndarray                    # 512-dim face embedding
    confidence: float                        # detection confidence 0-1
    metadata: Dict[str, Any]                 # optional: gender, age, landmarks

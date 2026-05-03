from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

from loguru import logger

try:
    # scenedetect's video_splitter module runs `FFMPEG_PATH = get_ffmpeg_path()` at
    # import time, which calls `subprocess.call(["ffmpeg", "-v", "quiet"])` to probe
    # for a system ffmpeg binary. On macOS with Homebrew, this can emit dyld errors to
    # stderr (e.g. unresolved @@HOMEBREW_CELLAR@@ library paths) even when ffmpeg works
    # fine for our purposes. Suppress fd-level stderr during the import to avoid the
    # noise — we use the OpenCV backend only and never call split_video_ffmpeg.
    _devnull_fd = os.open(os.devnull, os.O_WRONLY)
    _saved_stderr_fd = os.dup(2)
    os.dup2(_devnull_fd, 2)
    try:
        from scenedetect import detect, ContentDetector
    finally:
        os.dup2(_saved_stderr_fd, 2)
        os.close(_saved_stderr_fd)
        os.close(_devnull_fd)
except Exception as e:
    # Allow module import even if dependency missing; fail at runtime when used
    detect = None  # type: ignore
    ContentDetector = None  # type: ignore
    logger.warning("PySceneDetect not available. Install 'scenedetect[opencv]'.")


def detect_shots(
    video_path: Path | str,
    threshold: float = 30.0,
    min_scene_len: int = 15,
    frame_skip: int = 0,
) -> List[Tuple[float, float]]:
    """
    Detect shot boundaries using histogram-based content differences.

    Args:
        video_path: path to the video file
        threshold: sensitivity to visual change (27-35 typical; higher = fewer cuts)
        min_scene_len: minimum scene length in frames (15-30 typical)
        frame_skip: skip frames for speed (0 = none, 1-3 typical; currently unused in v0.6+ API)

    Returns:
        List of (t_start_sec, t_end_sec) for each detected shot.
    """
    if detect is None or ContentDetector is None:
        raise RuntimeError("PySceneDetect is not installed. Please install 'scenedetect[opencv]'.")

    logger.debug(f"Detecting shots path={video_path} threshold={threshold} min_scene_len={min_scene_len}")
    
    # Use new simplified API (v0.6+)
    detector = ContentDetector(threshold=threshold, min_scene_len=min_scene_len)
    scene_list = detect(str(video_path), detector, show_progress=False)
    
    logger.debug(f"PySceneDetect returned {len(scene_list)} scenes")

    shots: List[Tuple[float, float]] = []
    for idx, (start, end) in enumerate(scene_list):
        t0 = start.get_seconds()
        t1 = end.get_seconds()
        if t1 > t0:
            shots.append((t0, t1))
        else:
            logger.warning(f"Skipping invalid shot: start={t0} end={t1}")
    logger.debug(f"Detected valid shots count={len(shots)} from {len(scene_list)} scenes")
    return shots

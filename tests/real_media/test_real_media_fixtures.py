from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import cv2
from PIL import Image

from msa_indexer.io.scanner import iter_media
from msa_indexer.pipeline import VIDEO_EXT as PIPELINE_VIDEO_EXT
from msa_indexer.io.shot_detection import detect_shots
from msa_indexer.io.video import (
    extract_keyframes_from_shot,
    extract_video_frames,
    extract_video_gps_track,
    get_video_meta,
    likely_has_embedded_gps_track,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
ORIGINALS_DIR = FIXTURE_ROOT / "originals"
DERIVED_DIR = FIXTURE_ROOT / "derived"


EXPECTED_ORIGINALS = {
    "face_single_01.jpg",
    "face_single_02.jpg",
    "face_expressive_01.jpg",
    "face_group_01.jpg",
    "face_same_person_01.jpg",
    "face_same_person_02.jpg",
    "object_dog_01.jpg",
    "object_landscape_01.jpg",
    "video_face_single_01.webm",
    "video_people_crowd_01.webm",
    "video_street_objects_01.webm",
    "video_dog_object_01.webm",
    "video_dog_motion_01.webm",
}

EXPECTED_DERIVED = {
    "exif_gps_face_01.jpg",
    "exif_gps_face_01.heic",
    "exif_camera_face_02.jpg",
    "exif_face_expressive_01.jpg",
    "exif_face_same_person_01.jpg",
    "exif_face_same_person_02.jpg",
    "exif_group_faces_01.jpg",
    "exif_object_dog_01.jpg",
    "exif_object_landscape_01.jpg",
    "trimmed_video_dog_motion_01.webm",
    "trimmed_video_dog_object_01.webm",
    "trimmed_video_face_single_01.webm",
    "trimmed_gopro_gps_01.mp4",
    "trimmed_video_people_crowd_01.webm",
    "trimmed_video_street_objects_01.webm",
}


def _require_exiftool() -> None:
    if shutil.which("exiftool") is None:
        pytest.skip("exiftool is not installed")


def _duration_from_opencv(video_path: Path) -> float | None:
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS)
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if fps and fps > 0 and frame_count and frame_count > 0:
            return float(frame_count / fps)
        return None
    finally:
        cap.release()


@pytest.fixture(scope="module")
def originals_dir() -> Path:
    assert ORIGINALS_DIR.is_dir(), f"Missing originals fixture dir: {ORIGINALS_DIR}"
    return ORIGINALS_DIR


@pytest.fixture(scope="module")
def derived_dir() -> Path:
    assert DERIVED_DIR.is_dir(), f"Missing derived fixture dir: {DERIVED_DIR}"
    return DERIVED_DIR


class TestFixtureInventory:
    def test_expected_originals_exist(self, originals_dir: Path):
        actual = {path.name for path in originals_dir.iterdir() if path.is_file()}
        missing = EXPECTED_ORIGINALS - actual
        assert not missing, f"Missing original fixtures: {sorted(missing)}"

    def test_expected_derived_exist(self, derived_dir: Path):
        actual = {path.name for path in derived_dir.iterdir() if path.is_file()}
        missing = EXPECTED_DERIVED - actual
        assert not missing, f"Missing derived fixtures: {sorted(missing)}"

    def test_all_fixture_files_are_non_empty(self, originals_dir: Path, derived_dir: Path):
        for root in (originals_dir, derived_dir):
            for path in sorted(root.iterdir()):
                if path.is_file():
                    assert path.stat().st_size > 0, f"Fixture is empty: {path}"

    def test_scanner_finds_fixture_videos(self, originals_dir: Path, derived_dir: Path):
        paths = list(iter_media(FIXTURE_ROOT))
        video_names = {
            path.name for path in paths
            if path.suffix.lower() in {".webm", ".mp4"}
        }
        assert ".webm" in PIPELINE_VIDEO_EXT
        assert ".mp4" in PIPELINE_VIDEO_EXT
        assert "video_face_single_01.webm" in video_names
        assert "video_people_crowd_01.webm" in video_names
        assert "video_street_objects_01.webm" in video_names
        assert "trimmed_video_street_objects_01.webm" in video_names
        assert "trimmed_gopro_gps_01.mp4" in video_names

    def test_scanner_finds_fixture_heic(self, derived_dir: Path):
        paths = list(iter_media(FIXTURE_ROOT))
        image_names = {
            path.name for path in paths
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
        }
        assert "exif_gps_face_01.heic" in image_names


class TestImageFixtures:
    @pytest.mark.parametrize(
        "filename",
        [
            "face_single_01.jpg",
            "face_single_02.jpg",
            "face_expressive_01.jpg",
            "face_group_01.jpg",
            "face_same_person_01.jpg",
            "face_same_person_02.jpg",
            "object_dog_01.jpg",
            "object_landscape_01.jpg",
        ],
    )
    def test_original_images_open(self, originals_dir: Path, filename: str):
        with Image.open(originals_dir / filename) as img:
            img.load()
            assert img.width > 0
            assert img.height > 0

    @pytest.mark.parametrize(
        ("filename", "expected_make", "expected_model", "expected_lens"),
        [
            (
                "exif_gps_face_01.jpg",
                "Apple",
                "iPhone 15 Pro",
                "iPhone 15 Pro back triple camera 6.86mm f/1.78",
            ),
            (
                "exif_gps_face_01.heic",
                "Apple",
                "iPhone 15 Pro",
                "iPhone 15 Pro back triple camera 6.86mm f/1.78",
            ),
            (
                "exif_camera_face_02.jpg",
                "NIKON CORPORATION",
                "NIKON D850",
                "AF-S NIKKOR 70-200mm f/2.8E FL ED VR",
            ),
            (
                "exif_group_faces_01.jpg",
                "Canon",
                "Canon EOS R6",
                "RF24-70mm F2.8 L IS USM",
            ),
            (
                "exif_face_expressive_01.jpg",
                "Panasonic",
                "DC-S5M2",
                "LUMIX S 85/F1.8",
            ),
            (
                "exif_face_same_person_01.jpg",
                "Sony",
                "ILCE-7C",
                "FE 55mm F1.8 ZA",
            ),
            (
                "exif_face_same_person_02.jpg",
                "Sony",
                "ILCE-7C",
                "FE 85mm F1.8",
            ),
            (
                "exif_object_dog_01.jpg",
                "SONY",
                "ILCE-7M4",
                "FE 70-200mm F2.8 GM OSS II",
            ),
            (
                "exif_object_landscape_01.jpg",
                "FUJIFILM",
                "X-T5",
                "XF16-55mmF2.8 R LM WR",
            ),
        ],
    )
    def test_derived_images_have_expected_exif(
        self,
        derived_dir: Path,
        filename: str,
        expected_make: str,
        expected_model: str,
        expected_lens: str,
    ):
        import subprocess

        _require_exiftool()
        path = derived_dir / filename
        proc = subprocess.run(
            [
                "exiftool",
                "-s3",
                "-Make",
                "-Model",
                "-LensModel",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        values = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        assert values == [expected_make, expected_model, expected_lens]

    @pytest.mark.parametrize(
        "filename",
        [
            "exif_gps_face_01.jpg",
            "exif_group_faces_01.jpg",
            "exif_object_landscape_01.jpg",
        ],
    )
    def test_gps_derived_images_have_coordinates(self, derived_dir: Path, filename: str):
        import subprocess

        _require_exiftool()
        path = derived_dir / filename
        proc = subprocess.run(
            [
                "exiftool",
                "-n",
                "-s3",
                "-GPSLatitude",
                "-GPSLongitude",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        values = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        assert len(values) == 2
        lat, lon = map(float, values)
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon <= 180.0


class TestVideoFixtures:
    @pytest.mark.parametrize(
        "filename",
        [
            "video_face_single_01.webm",
            "video_people_crowd_01.webm",
            "video_street_objects_01.webm",
            "video_dog_object_01.webm",
            "video_dog_motion_01.webm",
            "trimmed_video_dog_motion_01.webm",
            "trimmed_video_dog_object_01.webm",
            "trimmed_video_face_single_01.webm",
            "trimmed_gopro_gps_01.mp4",
            "trimmed_video_people_crowd_01.webm",
            "trimmed_video_street_objects_01.webm",
        ],
    )
    def test_video_metadata_is_readable(self, originals_dir: Path, derived_dir: Path, filename: str):
        base_dir = derived_dir if filename.startswith("trimmed_") else originals_dir
        meta = get_video_meta(base_dir / filename)
        assert meta["duration"] is None or meta["duration"] > 0
        assert meta["width"] is None or meta["width"] > 0
        assert meta["height"] is None or meta["height"] > 0

    @pytest.mark.parametrize(
        "filename",
        [
            "video_face_single_01.webm",
            "video_people_crowd_01.webm",
            "video_street_objects_01.webm",
            "video_dog_object_01.webm",
            "video_dog_motion_01.webm",
            "trimmed_video_dog_motion_01.webm",
            "trimmed_video_dog_object_01.webm",
            "trimmed_video_face_single_01.webm",
            "trimmed_gopro_gps_01.mp4",
            "trimmed_video_people_crowd_01.webm",
            "trimmed_video_street_objects_01.webm",
        ],
    )
    def test_video_duration_fallback_is_available(self, originals_dir: Path, derived_dir: Path, filename: str):
        base_dir = derived_dir if filename.startswith("trimmed_") else originals_dir
        meta = get_video_meta(base_dir / filename)
        assert meta["duration"] is not None
        assert meta["duration"] > 0

    @pytest.mark.parametrize(
        "filename",
        [
            "video_face_single_01.webm",
            "video_dog_object_01.webm",
            "trimmed_video_street_objects_01.webm",
        ],
    )
    def test_video_frame_extraction_returns_frames(self, originals_dir: Path, derived_dir: Path, filename: str):
        base_dir = derived_dir if filename.startswith("trimmed_") else originals_dir
        frames = extract_video_frames(base_dir / filename, max_frames=3)
        assert len(frames) >= 1
        assert all(frame.width > 0 and frame.height > 0 for frame in frames)

    @pytest.mark.parametrize(
        "filename",
        [
            "video_people_crowd_01.webm",
            "video_street_objects_01.webm",
            "video_dog_motion_01.webm",
        ],
    )
    def test_scene_detection_and_keyframes_work(self, originals_dir: Path, filename: str):
        video_path = originals_dir / filename
        try:
            shots = detect_shots(video_path)
        except RuntimeError as exc:
            if "PySceneDetect is not installed" in str(exc):
                pytest.skip("PySceneDetect is not installed")
            raise
        if not shots:
            meta = get_video_meta(video_path)
            duration = meta.get("duration") or _duration_from_opencv(video_path)
            assert duration and duration > 0, f"Could not derive fallback shot for {video_path}"
            shots = [(0.0, float(duration))]

        keyframes = extract_keyframes_from_shot(
            video_path,
            shots[0],
            keyframes_per_shot=1,
        )
        assert len(keyframes) == 1
        timestamp, image = keyframes[0]
        assert timestamp >= 0
        if image is None:
            fallback_frames = extract_video_frames(video_path, max_frames=1, strategy="uniform")
            image = fallback_frames[0] if fallback_frames else None
        assert image is not None
        assert image.width > 0
        assert image.height > 0

    def test_gopro_gps_fixture_has_embedded_gps_track(self, derived_dir: Path):
        _require_exiftool()
        path = derived_dir / "trimmed_gopro_gps_01.mp4"
        assert likely_has_embedded_gps_track(path) is True

        samples = extract_video_gps_track(path)
        assert len(samples) >= 50
        assert samples[0]["gps_datetime_utc"] is not None
        assert samples[0]["gps_lat"] != samples[-1]["gps_lat"]
        assert samples[0]["gps_lon"] != samples[-1]["gps_lon"]

    def test_gopro_gps_fixture_does_not_expose_serial_fields(self, derived_dir: Path):
        import subprocess

        _require_exiftool()
        path = derived_dir / "trimmed_gopro_gps_01.mp4"
        proc = subprocess.run(
            ["exiftool", "-ee3", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        output = proc.stdout
        assert "Camera Serial Number" not in output
        assert "Lens Serial Number" not in output
        assert "Media Unique ID" not in output


@pytest.mark.slow
class TestModelBackedRealMediaChecks:
    def test_object_detection_on_dog_image(self, originals_dir: Path):
        import os
        from msa_indexer.models.objects import ObjectDetector

        workspace = os.environ.get("MSA_REALDATA_WORKSPACE")
        model_dir = Path(workspace) / "models" if workspace else None
        detector = ObjectDetector(
            model_name="PekingU/rtdetr_r18vd",
            device="cpu",
            conf_threshold=0.25,
            backend="rtdetr",
            model_dir=model_dir,
        )
        with Image.open(originals_dir / "object_dog_01.jpg") as img:
            labels = set(detector.get_labels(img.convert("RGB")))
        assert "dog" in labels

    def test_face_detection_on_single_portrait(self, originals_dir: Path):
        from msa_indexer.models.faces import FaceRecognizer

        recognizer = FaceRecognizer(device="cpu", conf_threshold=0.5, min_face_size=20)
        with Image.open(originals_dir / "face_single_01.jpg") as img:
            faces = recognizer.detect_and_embed(img.convert("RGB"))
        assert len(faces) >= 1

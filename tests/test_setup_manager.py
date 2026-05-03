"""
Tests for SetupManager in setup_models.py.

Covers correctness bugs fixed in code review:

  1. check_all_present() CLIP check is specific — no false positives from
     wrong-named .safetensors, random .pt files, or small blobs/ metadata files.

  2. check_all_present() facenet-pytorch check requires a vggface2 checkpoint
     file in the expected hub/checkpoints/ directory. The filename pattern uses
     "vggface2" as a substring (e.g. 20180402-114759-vggface2.pt), not a prefix.

  3. _download_worker() sets CLIP to error when open_clip_model.safetensors
     is absent after the download call — a partial HuggingFace cache is not
     silently accepted as done.

  4. start_if_needed() does not spawn a second worker thread while another
     download is still in the downloading or verifying state.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from msa_apps.search_api.setup_models import SetupManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manager_with_states(states: dict[str, str]) -> SetupManager:
    """Return a fresh SetupManager with _models pre-configured to given statuses."""
    mgr = SetupManager()
    for model_id, status in states.items():
        mgr._models[model_id].status = status
    mgr._download_started = True  # simulate a prior run
    return mgr


# ---------------------------------------------------------------------------
# 1. check_all_present — CLIP presence check
# ---------------------------------------------------------------------------

class TestCheckAllPresentClip:
    """check_all_present() must only report CLIP as present for the specific
    weight file (open_clip_model.safetensors) or a large blob — not for any
    random .pt / .safetensors file that happens to be in the cache dir."""

    def test_absent_when_no_files(self, tmp_path):
        assert SetupManager().check_all_present(tmp_path)["clip"] is False

    def test_present_with_correct_filename(self, tmp_path):
        nested = tmp_path / "clip" / "snapshots" / "abc123"
        nested.mkdir(parents=True)
        (nested / "open_clip_model.safetensors").write_bytes(b"x" * 1024)
        assert SetupManager().check_all_present(tmp_path)["clip"] is True

    def test_absent_with_wrong_safetensors_name(self, tmp_path):
        """model.safetensors or anything that isn't open_clip_model.safetensors
        must not satisfy the check — this was the old *.safetensors wildcard bug."""
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        (clip_dir / "model.safetensors").write_bytes(b"x" * 1024)
        assert SetupManager().check_all_present(tmp_path)["clip"] is False

    def test_absent_with_pt_file(self, tmp_path):
        """A stray .pt file must not count as CLIP present — this was the old *.pt wildcard bug."""
        clip_dir = tmp_path / "clip"
        clip_dir.mkdir()
        (clip_dir / "some_model.pt").write_bytes(b"x" * 1024)
        assert SetupManager().check_all_present(tmp_path)["clip"] is False

    def test_absent_with_small_blob(self, tmp_path):
        """A small file in blobs/ (tokeniser vocab, config JSON, etc.) must
        not count — only ≥100 MB blobs are the actual model weights."""
        blobs = tmp_path / "clip" / "blobs"
        blobs.mkdir(parents=True)
        (blobs / "abcdef01234567").write_bytes(b"x" * 1024)  # 1 KB
        assert SetupManager().check_all_present(tmp_path)["clip"] is False

    def test_present_with_large_blob(self, tmp_path):
        """A ≥100 MB file in blobs/ satisfies the Windows symlink-less layout."""
        blobs = tmp_path / "clip" / "blobs"
        blobs.mkdir(parents=True)
        large = blobs / "abcdef01234567"
        large.write_bytes(b"x" * (100 * 1024 * 1024 + 1))  # 100 MB + 1 byte
        assert SetupManager().check_all_present(tmp_path)["clip"] is True


# ---------------------------------------------------------------------------
# 2. check_all_present — facenet-pytorch presence check
# ---------------------------------------------------------------------------

class TestCheckAllPresentFacenet:
    """check_all_present() must report facenet_pytorch as present only when a
    vggface2 checkpoint file exists in facenet_pytorch/checkpoints/.
    facenet-pytorch reads TORCH_HOME directly (not torch.hub.get_dir()) and
    writes to ${TORCH_HOME}/checkpoints, so with TORCH_HOME set to
    models_dir/facenet_pytorch the weights land at
    models_dir/facenet_pytorch/checkpoints (no /hub segment). The actual filename
    from the release asset is 20180402-114759-vggface2.pt — "vggface2" is a
    substring, not a prefix, so the check must use 'in' not startswith."""

    def _checkpoints_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "facenet_pytorch" / "checkpoints"
        d.mkdir(parents=True)
        return d

    def test_present_with_torch_hub_filename(self, tmp_path):
        """Standard torch hub filename has a timestamp prefix."""
        d = self._checkpoints_dir(tmp_path)
        (d / "20180402-114759-vggface2.pt").write_bytes(b"fake")
        assert SetupManager().check_all_present(tmp_path)["facenet_pytorch"] is True

    def test_present_with_vggface2_prefix_filename(self, tmp_path):
        """Also accept names where vggface2 is a prefix (alternative download)."""
        d = self._checkpoints_dir(tmp_path)
        (d / "vggface2-abcdef.pt").write_bytes(b"fake")
        assert SetupManager().check_all_present(tmp_path)["facenet_pytorch"] is True

    def test_absent_when_checkpoints_dir_missing(self, tmp_path):
        assert SetupManager().check_all_present(tmp_path)["facenet_pytorch"] is False

    def test_absent_when_no_pt_file(self, tmp_path):
        d = self._checkpoints_dir(tmp_path)
        (d / "vggface2.json").write_bytes(b"fake")
        assert SetupManager().check_all_present(tmp_path)["facenet_pytorch"] is False

    def test_absent_when_unrelated_pt_file(self, tmp_path):
        """A .pt file with an unrelated name must not satisfy the check."""
        d = self._checkpoints_dir(tmp_path)
        (d / "casia-webface.pt").write_bytes(b"fake")
        assert SetupManager().check_all_present(tmp_path)["facenet_pytorch"] is False


# ---------------------------------------------------------------------------
# 3. _download_worker — bootstrap failure marks all models error and completes
# ---------------------------------------------------------------------------

class TestDownloadWorkerBootstrapFailure:
    """If models_dir cannot be created (e.g. unwritable path), the worker must
    mark every pending model as error and set _complete=True so the WebSocket
    loop terminates and the UI can show the error rather than polling forever."""

    def test_unwritable_models_dir_marks_all_error_and_completes(self):
        mgr = SetupManager()
        bad_dir = Path("/nonexistent_root_xyz/models")
        # Simulate unwritable path cross-platform — on Windows the unix-style
        # path resolves to a real drive path the runner can write to.
        with patch.object(Path, "mkdir", side_effect=PermissionError("read-only filesystem")):
            mgr._download_worker(bad_dir)

        assert mgr.is_complete(), "_complete must be True so the WS loop exits"
        for model_id, state in mgr._models.items():
            assert state.status == "error", f"{model_id} should be error, got {state.status}"
            assert state.error is not None, f"{model_id} error message must be set"


# ---------------------------------------------------------------------------
# 4. _download_worker — CLIP weight file must exist after download
# ---------------------------------------------------------------------------

class TestClipDownloadWorkerVerification:
    """If open_clip finishes without writing open_clip_model.safetensors (e.g.
    only tokeniser metadata was downloaded), the worker must error rather than
    silently advancing CLIP to done."""

    def _run_worker(self, tmp_path: Path, write_weight_file: bool) -> SetupManager:
        mgr = SetupManager()
        models_dir = tmp_path / "models"

        def fake_create_model(model_name, pretrained, device, cache_dir):
            if write_weight_file:
                dst = Path(cache_dir) / "open_clip_model.safetensors"
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(b"fake_weights")
            return MagicMock(), MagicMock(), MagicMock()

        mock_oc = MagicMock()
        mock_oc.create_model_and_transforms.side_effect = fake_create_model

        # Mock facenet_pytorch so no real download or model load occurs.
        mock_fp = MagicMock()
        mock_resnet = MagicMock()
        mock_resnet.eval.return_value = mock_resnet
        mock_fp.InceptionResnetV1.return_value = mock_resnet
        mock_fp.MTCNN.return_value = MagicMock()

        with (
            patch.dict(sys.modules, {
                "open_clip": mock_oc,
                "facenet_pytorch": mock_fp,
            }),
            # Skip real hash computation — focus on the missing-file guard
            patch("msa_apps.search_api.setup_models._verify_file", return_value=True),
        ):
            mgr._download_worker(models_dir)

        return mgr

    def test_clip_errors_when_weight_file_missing(self, tmp_path):
        mgr = self._run_worker(tmp_path, write_weight_file=False)
        assert mgr._models["clip"].status == "error"
        assert mgr._models["clip"].error is not None
        assert "not found" in mgr._models["clip"].error.lower()

    def test_clip_done_when_weight_file_present_and_valid(self, tmp_path):
        mgr = self._run_worker(tmp_path, write_weight_file=True)
        assert mgr._models["clip"].status == "done"
        assert mgr._models["clip"].error is None


# ---------------------------------------------------------------------------
# 3. start_if_needed — no second thread while downloads are in progress
# ---------------------------------------------------------------------------

class TestStartIfNeededThreadGuard:
    """start_if_needed() must not spawn a second worker thread while any model
    is still downloading or verifying.  A WS reconnect arriving mid-run (e.g.
    because CLIP errored early while RT-DETR was still running) must not create
    races on disk."""

    def test_no_thread_when_model_is_downloading(self, tmp_path):
        mgr = _manager_with_states({
            "clip": "error",
            "rtdetr": "downloading",
            "facenet_pytorch": "pending",
        })
        with patch("threading.Thread") as mock_thread:
            mgr.start_if_needed(tmp_path)
            mock_thread.assert_not_called()

    def test_no_thread_when_model_is_verifying(self, tmp_path):
        mgr = _manager_with_states({
            "clip": "error",
            "rtdetr": "done",
            "facenet_pytorch": "verifying",
        })
        with patch("threading.Thread") as mock_thread:
            mgr.start_if_needed(tmp_path)
            mock_thread.assert_not_called()

    def test_thread_spawned_when_all_settled_with_error(self, tmp_path):
        """Once every model has settled (none in-progress), a retry is allowed."""
        mgr = _manager_with_states({
            "clip": "error",
            "rtdetr": "done",
            "facenet_pytorch": "done",
        })
        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            mgr.start_if_needed(tmp_path)
            mock_thread.assert_called_once()

    def test_no_thread_when_all_done(self, tmp_path):
        """All models done, no errors — nothing to do."""
        mgr = _manager_with_states({
            "clip": "done",
            "rtdetr": "done",
            "facenet_pytorch": "done",
        })
        with patch("threading.Thread") as mock_thread:
            mgr.start_if_needed(tmp_path)
            mock_thread.assert_not_called()

    def test_errored_model_reset_to_pending_on_retry(self, tmp_path):
        """When a retry thread is spawned, errored models are reset to pending
        and their error message is cleared."""
        mgr = _manager_with_states({
            "clip": "error",
            "rtdetr": "done",
            "facenet_pytorch": "done",
        })
        mgr._models["clip"].error = "SHA-256 mismatch for CLIP weights"

        with patch("threading.Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            mgr.start_if_needed(tmp_path)

        assert mgr._models["clip"].status == "pending"
        assert mgr._models["clip"].error is None
        # Non-errored models must not be touched
        assert mgr._models["rtdetr"].status == "done"
        assert mgr._models["facenet_pytorch"].status == "done"

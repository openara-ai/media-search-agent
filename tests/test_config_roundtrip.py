"""
Verify that the three config-write API endpoints (POST/DELETE /config/sources,
PATCH /config/model) preserve comments, key order, and quote styles in
config.yaml — i.e. the file remains human-readable after a Settings-page save.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Canonical fixture config — mirrors the repo-root config.yaml style
# ---------------------------------------------------------------------------

_FIXTURE_CONFIG = textwrap.dedent("""\
    # Media Search Agent — Development Configuration
    # Read-only: edit this file to configure the indexer.

    # ── Media sources ─────────────────────────────────────────────────────────
    media_sources: []

    # ── CLIP model ────────────────────────────────────────────────────────────
    model_name: "ViT-L-14"
    pretrained: "openai"

    # ── Compute ───────────────────────────────────────────────────────────────
    device: "auto"
    batch_size: 32

    # ── Object detection ──────────────────────────────────────────────────────
    # auto = GPU only; true = always; false = disabled
    enable_object_detection: auto
    object_detector_backend: "rtdetr"
    object_model: "PekingU/rtdetr_r18vd"
    object_confidence_threshold: 0.35

    # ── Face recognition ──────────────────────────────────────────────────────
    enable_face_recognition: true
    face_model: "buffalo_l"
    face_confidence_threshold: 0.7
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lines(text: str) -> list[str]:
    return text.splitlines()


def _has_comment(text: str, fragment: str) -> bool:
    return any(fragment in line for line in _lines(text) if line.lstrip().startswith("#"))


def _key_order(text: str) -> list[str]:
    """Return the non-comment, non-blank top-level keys in file order."""
    keys = []
    for line in _lines(text):
        if line and not line.startswith(" ") and not line.startswith("#"):
            key = line.split(":")[0].strip()
            if key:
                keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(_FIXTURE_CONFIG, encoding="utf-8")
    return p


@pytest.fixture()
def api_client(cfg_file: Path, tmp_path: Path, monkeypatch):
    """TestClient wired so _config_path() resolves to cfg_file."""
    from msa_apps.search_api.app import create_app

    (tmp_path / "thumbnails").mkdir()
    (tmp_path / "face_thumbnails").mkdir()
    (tmp_path / "logs").mkdir()

    test_config = SimpleNamespace(
        sqlite_path=str(tmp_path / "test.db"),
        server=SimpleNamespace(qdrant_url="http://localhost:6333", qdrant_api_key=None),
        collections=SimpleNamespace(face="face_emb"),
        thumb_dir=tmp_path / "thumbnails",
        face_thumb_dir=tmp_path / "face_thumbnails",
        log_dir="logs",
        log_level="DEBUG",
        media_sources=[],
        qdrant_port=6333,
    )

    monkeypatch.chdir(tmp_path)
    app = create_app(config_override=test_config)
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /config/sources — add a media source
# ---------------------------------------------------------------------------

class TestAddSourcePreservesConfig:
    def test_section_comments_survive_add(self, api_client, cfg_file):
        api_client.post("/config/sources", json={
            "name": "holiday",
            "path": "/mnt/d/Holiday",
            "read_only": True,
            "description": "",
        })
        text = cfg_file.read_text()
        assert _has_comment(text, "Media sources")
        assert _has_comment(text, "CLIP model")
        assert _has_comment(text, "Object detection")
        assert _has_comment(text, "auto = GPU only")

    def test_key_order_preserved_after_add(self, api_client, cfg_file):
        before = _key_order(cfg_file.read_text())
        api_client.post("/config/sources", json={
            "name": "holiday",
            "path": "/mnt/d/Holiday",
            "read_only": True,
            "description": "",
        })
        after = _key_order(cfg_file.read_text())
        # All original top-level keys still present in the same relative order
        original_in_after = [k for k in after if k in before]
        assert original_in_after == before

    def test_quoted_strings_preserved_after_add(self, api_client, cfg_file):
        api_client.post("/config/sources", json={
            "name": "holiday",
            "path": "/mnt/d/Holiday",
            "read_only": True,
            "description": "",
        })
        text = cfg_file.read_text()
        assert 'model_name: "ViT-L-14"' in text
        assert 'object_model: "PekingU/rtdetr_r18vd"' in text
        assert 'face_model: "buffalo_l"' in text

    def test_new_source_written_to_file(self, api_client, cfg_file):
        api_client.post("/config/sources", json={
            "name": "holiday",
            "path": "/mnt/d/Holiday",
            "read_only": True,
            "description": "",
        })
        text = cfg_file.read_text()
        assert "holiday" in text
        assert "/mnt/d/Holiday" in text


# ---------------------------------------------------------------------------
# DELETE /config/sources/{name} — remove a media source
# ---------------------------------------------------------------------------

class TestDeleteSourcePreservesConfig:
    @pytest.fixture(autouse=True)
    def seed_source(self, api_client):
        api_client.post("/config/sources", json={
            "name": "seed",
            "path": "/mnt/d/Seed",
            "read_only": True,
            "description": "",
        })

    def test_section_comments_survive_delete(self, api_client, cfg_file):
        api_client.delete("/config/sources/seed")
        text = cfg_file.read_text()
        assert _has_comment(text, "Media sources")
        assert _has_comment(text, "CLIP model")
        assert _has_comment(text, "Object detection")

    def test_key_order_preserved_after_delete(self, api_client, cfg_file):
        before = _key_order(_FIXTURE_CONFIG)
        api_client.delete("/config/sources/seed")
        after = _key_order(cfg_file.read_text())
        original_in_after = [k for k in after if k in before]
        assert original_in_after == before

    def test_quoted_strings_preserved_after_delete(self, api_client, cfg_file):
        api_client.delete("/config/sources/seed")
        text = cfg_file.read_text()
        assert 'model_name: "ViT-L-14"' in text
        assert 'object_model: "PekingU/rtdetr_r18vd"' in text


# ---------------------------------------------------------------------------
# PATCH /config/model — update model settings
# ---------------------------------------------------------------------------

class TestPatchModelPreservesConfig:
    def test_section_comments_survive_patch(self, api_client, cfg_file):
        api_client.patch("/config/model", json={"batch_size": 16})
        text = cfg_file.read_text()
        assert _has_comment(text, "CLIP model")
        assert _has_comment(text, "Object detection")
        assert _has_comment(text, "auto = GPU only")
        assert _has_comment(text, "Face recognition")

    def test_key_order_preserved_after_patch(self, api_client, cfg_file):
        before = _key_order(cfg_file.read_text())
        api_client.patch("/config/model", json={"batch_size": 16})
        after = _key_order(cfg_file.read_text())
        original_in_after = [k for k in after if k in before]
        assert original_in_after == before

    def test_quoted_strings_preserved_after_patch(self, api_client, cfg_file):
        api_client.patch("/config/model", json={"batch_size": 16})
        text = cfg_file.read_text()
        assert 'model_name: "ViT-L-14"' in text
        assert 'object_model: "PekingU/rtdetr_r18vd"' in text
        assert 'face_model: "buffalo_l"' in text

    def test_unrelated_keys_untouched_after_patch(self, api_client, cfg_file):
        api_client.patch("/config/model", json={"batch_size": 16})
        text = cfg_file.read_text()
        # Keys not in the patch body must be unchanged
        assert "pretrained" in text
        assert "device" in text
        assert "enable_face_recognition" in text

    def test_patched_value_written_correctly(self, api_client, cfg_file):
        api_client.patch("/config/model", json={"batch_size": 16})
        text = cfg_file.read_text()
        assert "batch_size: 16" in text

    def test_multiple_fields_patched_simultaneously(self, api_client, cfg_file):
        api_client.patch("/config/model", json={
            "batch_size": 8,
            "face_confidence_threshold": 0.8,
        })
        text = cfg_file.read_text()
        assert "batch_size: 8" in text
        assert "face_confidence_threshold: 0.8" in text
        assert _has_comment(text, "CLIP model")
        assert _has_comment(text, "Object detection")

"""
Tests for platform-specific config templates.

Validates that:
- Both checked-in templates parse as valid YAML
- Required keys are present with sane defaults
- Templates load cleanly via the Config dataclass

Run after any edit to installer/*/config.*.yaml.template:
    pytest tests/test_config_templates.py -v
"""
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# Linux end-user installer is a future platform — add config.linux.yaml.template
# here when installer/linux/ is created.
TEMPLATES = {
    "macos": REPO_ROOT / "installer/macos/config.macos.yaml.template",
    "windows": REPO_ROOT / "installer/windows-native/config.windows.yaml.template",
}

REQUIRED_KEYS = [
    "media_sources",
    "model_name",
    "pretrained",
    "device",
    "batch_size",
    "num_workers",
    "enable_object_detection",
    "enable_face_recognition",
    "enable_video_shot_detection",
    "log_level",
    "api",
    "retrieval",
    "server",
]


@pytest.fixture(params=list(TEMPLATES.keys()))
def template(request):
    name = request.param
    path = TEMPLATES[name]
    assert path.exists(), f"Template not found: {path}"
    return name, path


def test_template_exists(template):
    _, path = template
    assert path.exists()


def test_template_valid_yaml(template):
    _, path = template
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        pytest.fail(f"{path.name}: YAML parse error: {e}")


def test_template_required_keys(template):
    _, path = template
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED_KEYS if k not in data]
    assert not missing, f"{path.name}: missing keys: {missing}"


def test_template_api_port_8000(template):
    _, path = template
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    port = (data.get("api") or {}).get("port")
    assert port == 8000, f"{path.name}: expected api.port=8000, got {port}"


def test_template_media_sources_empty(template):
    """Factory default must ship with no sources — user must add their own."""
    _, path = template
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = data.get("media_sources", [])
    assert sources == [] or sources is None, (
        f"{path.name}: media_sources should be empty in the template, got {sources}"
    )


def test_template_device_auto(template):
    _, path = template
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert data.get("device") == "auto", (
        f"{path.name}: device should be 'auto', got {data.get('device')}"
    )


def test_template_loads_via_config_dataclass(template):
    """Template must produce a valid Config when loaded through the real loader."""
    _, path = template
    from msa_settings.config import _load_config_impl, load_config
    load_config.cache_clear()
    try:
        cfg = _load_config_impl(str(path))
        assert cfg.api.port == 8000
        assert cfg.device in ("auto", "cpu", "cuda", "mps")
    finally:
        load_config.cache_clear()


def test_windows_template_face_model():
    """Windows template should use vggface2 (facenet-pytorch default)."""
    path = TEMPLATES["windows"]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert data.get("face_model") == "vggface2", (
        f"Windows template should use vggface2, got {data.get('face_model')}"
    )


def test_macos_template_face_model():
    """macOS template should use vggface2 (facenet-pytorch default)."""
    path = TEMPLATES["macos"]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert data.get("face_model") == "vggface2", (
        f"macOS template should use vggface2, got {data.get('face_model')}"
    )

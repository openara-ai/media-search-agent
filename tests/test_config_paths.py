"""
Tests for Phase 2F Items 1, 2 & 4:
  - Absolute data directory path resolution (_resolve_data_paths)
  - Config file discovery and first-launch bootstrap (_find_config_path)
  - Device auto-detection (detect_device)
"""
import os
import sys
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload(monkeypatch, tmp_path, env: dict):
    """Clear the lru_cache, set env vars, and reload config from a temp config file."""
    from msa_settings import config as cfg_mod
    cfg_mod.load_config.cache_clear()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Write a minimal valid config to tmp_path
    config_file = tmp_path / "config.yaml"
    config_file.write_text("log_level: INFO\n")
    return cfg_mod._load_config_impl(str(config_file))


# ---------------------------------------------------------------------------
# _resolve_data_paths — Item 1
# ---------------------------------------------------------------------------

class TestResolveDataPaths:
    def test_dev_mode_resolves_relative_to_repo_root(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.setenv("MSA_DEV", "1")
        monkeypatch.delenv("MSA_DATA_DIR", raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        assert cfg.index_dir.is_absolute()
        assert cfg.index_dir == (cfg_mod.REPO_ROOT / "index").resolve()
        assert cfg.sqlite_path == (cfg_mod.REPO_ROOT / "index/media.sqlite").resolve()
        assert cfg.thumb_dir == (cfg_mod.REPO_ROOT / "data/thumbnails").resolve()

    def test_msa_data_dir_overrides_platform_default(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_DEV", raising=False)
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "mydata"))

        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        data_root = tmp_path / "mydata"
        assert cfg.index_dir == (data_root / "index").resolve()
        assert cfg.sqlite_path == (data_root / "index/media.sqlite").resolve()
        assert cfg.thumb_dir == (data_root / "data/thumbnails").resolve()
        assert cfg.face_thumb_dir == (data_root / "data/face_thumbnails").resolve()
        # log_dir is NOT under MSA_DATA_DIR — it resolves via MSA_LOG_DIR independently
        assert cfg.log_dir != (data_root / "logs").resolve()

    def test_msa_log_dir_overrides_platform_default(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_DEV", raising=False)
        monkeypatch.delenv("MSA_DATA_DIR", raising=False)
        log_dir = tmp_path / "AppDir" / "logs"
        monkeypatch.setenv("MSA_LOG_DIR", str(log_dir))

        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        # MSA_LOG_DIR is used directly as cfg.log_dir (not as a root to append "logs" under)
        assert cfg.log_dir == log_dir.resolve()

    def test_all_data_paths_are_absolute(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_DEV", raising=False)
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))

        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        for field_name in cfg_mod._DATA_PATH_FIELDS:
            p = getattr(cfg, field_name)
            assert p.is_absolute(), f"{field_name} is not absolute: {p}"

    def test_explicit_absolute_path_in_config_is_preserved(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_DEV", raising=False)
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))

        abs_sqlite = str(tmp_path / "custom/media.sqlite")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(f"sqlite_path: '{abs_sqlite}'\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        # Absolute path in config should not be remapped under MSA_DATA_DIR
        assert cfg.sqlite_path == Path(abs_sqlite)

    def test_platform_default_used_when_no_env(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_DEV", raising=False)
        monkeypatch.delenv("MSA_DATA_DIR", raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        expected_root = cfg_mod._platform_data_dir()
        assert cfg.index_dir == (expected_root / "index").resolve()


# ---------------------------------------------------------------------------
# _find_config_path — Item 2
# ---------------------------------------------------------------------------

class TestFindConfigPath:
    def test_msa_config_path_env_used_when_set(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()

        cfg_file = tmp_path / "my_config.yaml"
        cfg_file.write_text("log_level: DEBUG\n")
        monkeypatch.setenv("MSA_CONFIG_PATH", str(cfg_file))
        monkeypatch.delenv("MSA_DATA_DIR", raising=False)

        result = cfg_mod._find_config_path()
        assert result == str(cfg_file)

    def test_msa_config_path_missing_file_raises(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        monkeypatch.setenv("MSA_CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))

        with pytest.raises(FileNotFoundError, match="MSA_CONFIG_PATH"):
            cfg_mod._find_config_path()

    def test_platform_config_dir_used_when_present(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_CONFIG_PATH", raising=False)

        platform_cfg = tmp_path / "config.yaml"
        platform_cfg.write_text("log_level: INFO\n")
        monkeypatch.setattr(cfg_mod, "_platform_config_dir", lambda: tmp_path)

        result = cfg_mod._find_config_path()
        assert result == str(platform_cfg)

    def test_repo_root_fallback_used_in_dev(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_CONFIG_PATH", raising=False)
        # Platform config dir has no config.yaml
        monkeypatch.setattr(cfg_mod, "_platform_config_dir", lambda: tmp_path / "empty")

        # REPO_ROOT/config.yaml exists (it does in the dev repo)
        result = cfg_mod._find_config_path()
        assert result == str(cfg_mod.REPO_ROOT / "config.yaml")

    def test_raises_when_nothing_found(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.delenv("MSA_CONFIG_PATH", raising=False)
        monkeypatch.setattr(cfg_mod, "_platform_config_dir", lambda: tmp_path / "msa")
        monkeypatch.setattr(cfg_mod, "REPO_ROOT", tmp_path)

        with pytest.raises(FileNotFoundError, match="config.yaml not found"):
            cfg_mod._find_config_path()


# ---------------------------------------------------------------------------
# Config file encoding — BOM handling (PowerShell 5.1 writes UTF-8 BOM)
# ---------------------------------------------------------------------------

class TestConfigFileEncoding:
    """Regression tests for UTF-8 BOM handling.

    PowerShell 5.1's Set-Content -Encoding UTF8 writes a BOM (EF BB BF).
    Python's open() without an explicit encoding uses the system locale
    (cp1252 on Windows), which decodes the BOM three bytes as 'ï»¿',
    causing PyYAML to fail with "expected '<document start>'".

    On Linux the default encoding is UTF-8, so the same bytes decode as
    the single Unicode character U+FEFF which PyYAML silently accepts —
    meaning the bug is invisible on Linux without explicit cp1252 simulation.

    The fix is encoding='utf-8-sig', which strips the BOM before YAML sees it.
    The Windows behaviour is reproduced in tests by patching builtins.open to
    inject cp1252 when no explicit encoding is provided — exactly what Windows
    does. The fix's explicit encoding='utf-8-sig' bypasses the patch, making
    the tests deterministically fail before the fix and pass after.
    """

    # ── root-cause documentation ────────────────────────────────────────────

    def test_cp1252_decoded_bom_breaks_yaml(self):
        """Documents the root cause: cp1252 BOM bytes corrupt the YAML stream.

        This test does NOT call _load_config_impl — it isolates the YAML-level
        failure so the cause is unambiguous.
        """
        import yaml as _yaml

        # The three UTF-8 BOM bytes decoded with cp1252 (Windows default locale)
        bom_as_cp1252 = b"\xef\xbb\xbf".decode("cp1252")  # produces 'ï»¿'
        assert bom_as_cp1252 == "ï»¿"

        # Template that starts with comment lines — required to trigger the error;
        # a plain mapping key would be silently concatenated (e.g. 'ï»¿key: val').
        corrupted = bom_as_cp1252 + "# comment\n\nmedia_sources: []\n"
        with pytest.raises(_yaml.YAMLError):
            _yaml.safe_load(corrupted)

    # ── regression tests against _load_config_impl ──────────────────────────

    @staticmethod
    def _bom_config(tmp_path):
        """Write a BOM-prefixed config that starts with comment lines (the installer template shape)."""
        config_file = tmp_path / "config.yaml"
        bom = b"\xef\xbb\xbf"
        content = (
            b"# Media Search Agent config\n"
            b"# comment line 2\n\n"
            b"media_sources: []\n"
            b"model_name: 'ViT-L-14'\n"
            b"api:\n"
            b"  port: 8000\n"
        )
        config_file.write_bytes(bom + content)
        return config_file

    def test_bom_file_loads_correctly(self, monkeypatch, tmp_path):
        """_load_config_impl must parse a BOM-prefixed config.yaml without error.

        Uses a cp1252-injecting open() patch to simulate Windows encoding behaviour
        cross-platform. The fix (encoding='utf-8-sig') provides an explicit encoding
        that bypasses the patch and strips the BOM, so this test:
          - FAILS without the fix  (cp1252 injected → YAML parse error)
          - PASSES with the fix    (utf-8-sig strips BOM before YAML sees it)
        """
        import builtins
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))

        config_file = self._bom_config(tmp_path)

        real_open = builtins.open

        def _simulate_windows_encoding(path, mode="r", **kwargs):
            """Inject cp1252 when no encoding is given, mimicking Windows open() default."""
            if "b" not in str(mode) and "encoding" not in kwargs:
                kwargs["encoding"] = "cp1252"
            return real_open(path, mode, **kwargs)

        monkeypatch.setattr(builtins, "open", _simulate_windows_encoding)

        cfg = cfg_mod._load_config_impl(str(config_file))
        assert cfg.media_sources == []
        assert cfg.api.port == 8000

    def test_config_without_bom_still_works(self, monkeypatch, tmp_path):
        """Non-BOM UTF-8 (the normal case on Linux/macOS) continues to work."""
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))

        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: WARNING\nmedia_sources: []\n", encoding="utf-8")

        cfg = cfg_mod._load_config_impl(str(config_file))
        assert cfg.log_level == "WARNING"


# ---------------------------------------------------------------------------
# Platform directory helpers
# ---------------------------------------------------------------------------

class TestPlatformDirs:
    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_linux_data_dir_ignores_xdg(self, monkeypatch, tmp_path):
        # Per ADR-009, runtime intentionally does NOT honor XDG_DATA_HOME on
        # Linux — it must match the installer, which hardcodes ~/.local/share.
        # Power users override via MSA_DATA_DIR, not XDG.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/xdg")
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_data_dir()
        assert result == tmp_path / ".local" / "share" / "MediaSearchAgent"

    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_linux_config_dir_ignores_xdg(self, monkeypatch, tmp_path):
        # Per ADR-009, runtime intentionally does NOT honor XDG_CONFIG_HOME.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/config")
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_config_dir()
        assert result == tmp_path / ".config" / "MediaSearchAgent"

    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_linux_cache_dir_ignores_xdg(self, monkeypatch, tmp_path):
        # Per ADR-009, runtime intentionally does NOT honor XDG_CACHE_HOME and
        # uses MediaSearchAgent (not the legacy 'msa' name) on Linux.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setenv("XDG_CACHE_HOME", "/custom/cache")
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_cache_dir()
        assert result == tmp_path / ".cache" / "MediaSearchAgent"

    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_linux_log_dir_uses_local_share(self, monkeypatch, tmp_path):
        # Per ADR-009, runtime log dir on Linux is ~/.local/share/MediaSearchAgent/logs
        # and intentionally does NOT honor XDG_DATA_HOME.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_log_dir()
        assert result == tmp_path / ".local" / "share" / "MediaSearchAgent" / "logs"

    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_macos_data_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os, "name", "posix")

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_data_dir()
        assert result == Path.home() / "Library" / "Application Support" / "MediaSearchAgent"

    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_macos_config_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os, "name", "posix")

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_config_dir()
        assert result == Path.home() / "Library" / "Application Support" / "MediaSearchAgent"

    @pytest.mark.skipif(os.name == "nt", reason="PosixPath cannot be instantiated on Windows")
    def test_macos_cache_dir(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os, "name", "posix")

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_cache_dir()
        assert result == Path.home() / "Library" / "Caches" / "MediaSearchAgent"

    @pytest.mark.skipif(os.name != "nt", reason="WindowsPath cannot be instantiated on non-Windows")
    def test_windows_data_dir_uses_home(self, monkeypatch, tmp_path):
        # _platform_data_dir() on Windows uses Path.home() / 'MediaSearchAgent',
        # not APPDATA. Mock Path.home to isolate from the real user home.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_data_dir()
        assert result == tmp_path / "MediaSearchAgent"

    @pytest.mark.skipif(os.name != "nt", reason="WindowsPath cannot be instantiated on non-Windows")
    def test_windows_config_dir_uses_home(self, monkeypatch, tmp_path):
        # _platform_config_dir() on Windows uses Path.home() / 'MediaSearchAgent'
        # (matching the installer's $DataDir default), not APPDATA.
        # Regression guard for the runtime/installer alignment per ADR-009.
        from pathlib import Path as _Path
        monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))

        from msa_settings import config as cfg_mod
        result = cfg_mod._platform_config_dir()
        assert result == tmp_path / "MediaSearchAgent"


# ---------------------------------------------------------------------------
# detect_device — Item 4
# ---------------------------------------------------------------------------

class TestDetectDevice:
    def test_returns_cuda_when_cuda_available(self, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)

        from msa_settings.config import detect_device
        assert detect_device() == "cuda"

    def test_returns_mps_when_cuda_unavailable_and_mps_available(self, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)

        from msa_settings.config import detect_device
        assert detect_device() == "mps"

    def test_returns_cpu_when_no_accelerator(self, monkeypatch):
        import torch
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)

        from msa_settings.config import detect_device
        assert detect_device() == "cpu"

    def test_returns_cpu_when_torch_not_installed(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def _block_torch(name, *args, **kwargs):
            if name == "torch":
                raise ImportError("torch not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_torch)

        # Re-import to get an unbound reference (can't use cached import)
        import importlib, msa_settings.config as cfg_mod
        assert cfg_mod.detect_device() == "cpu"

    def test_auto_in_config_resolves_to_real_device(self, monkeypatch, tmp_path):
        """device: auto in config.yaml must not survive load_config — it must be resolved."""
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv("DEVICE", raising=False)

        config_file = tmp_path / "config.yaml"
        config_file.write_text("device: auto\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        assert cfg.device != "auto"
        assert cfg.device in ("cuda", "mps", "cpu")

    def test_device_env_var_overrides_auto(self, monkeypatch, tmp_path):
        from msa_settings import config as cfg_mod
        cfg_mod.load_config.cache_clear()
        monkeypatch.setenv("MSA_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("DEVICE", "cpu")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("device: auto\n")
        cfg = cfg_mod._load_config_impl(str(config_file))

        assert cfg.device == "cpu"

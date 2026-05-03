import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_start_does_not_force_export_to_qdrant(tmp_path, monkeypatch):
    from msa_apps.search_api import indexer_manager as mod

    monkeypatch.setattr(mod.IndexerManager, "_restore_from_pid_file", lambda self: None)

    manager = mod.IndexerManager()

    log_dir = tmp_path / "logs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("media_sources: []\n")

    fake_thread = MagicMock()
    fake_thread.start = MagicMock()
    fake_proc = MagicMock()
    fake_proc.pid = 12345

    with patch("msa_settings.load_config", return_value=SimpleNamespace(log_dir=log_dir)), \
         patch("msa_query.storage.qdrant_client.close_shared_client"), \
         patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
         patch("threading.Thread", return_value=fake_thread):
        result = manager.start(str(config_path), str(tmp_path / ".venv" / "bin"))

    cmd = mock_popen.call_args.args[0]

    assert result["status"] == "running"
    assert "--export-to-qdrant" not in cmd
    assert cmd[-1] == "--no-console-log"
    script_name = "msa.exe" if sys.platform == "win32" else "msa"
    assert cmd[:4] == [
        str(Path(tmp_path / ".venv" / "bin") / script_name),
        "index",
        "run",
        "--config",
    ]

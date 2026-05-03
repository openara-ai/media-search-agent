"""
Tests for Phase 2F Item 8 — single instance enforcement.

acquire_instance_lock() must:
  - Block startup (SystemExit) when a live PID holds the lock
  - Clean up and proceed when a dead PID holds the lock
  - Create the lock file with our PID on success
  - Be released cleanly by release_instance_lock()
"""
import os
import pytest
from pathlib import Path

from msa_settings.instance_lock import acquire_instance_lock, release_instance_lock


class TestAcquireInstanceLock:
    def test_no_lock_file_succeeds(self, tmp_path):
        """No existing lock → acquires successfully and writes our PID."""
        lock = tmp_path / "test.lock"
        acquire_instance_lock(lock, "TestApp")
        assert lock.exists()
        assert int(lock.read_text().strip()) == os.getpid()

    def test_live_pid_blocks_startup(self, tmp_path):
        """Lock file containing a live PID → raises SystemExit."""
        lock = tmp_path / "test.lock"
        lock.write_text(str(os.getpid()))  # our own PID is definitely alive
        with pytest.raises(SystemExit, match="already running"):
            acquire_instance_lock(lock, "TestApp")

    def test_dead_pid_cleans_up_and_succeeds(self, tmp_path):
        """Lock file with a non-existent PID → stale lock removed, acquisition succeeds."""
        lock = tmp_path / "test.lock"
        # PID 99999 is virtually guaranteed to not exist
        lock.write_text("99999")
        acquire_instance_lock(lock, "TestApp")
        # Lock now contains our PID
        assert int(lock.read_text().strip()) == os.getpid()

    def test_corrupt_lock_file_is_overwritten(self, tmp_path):
        """Lock file with garbage content → treated as no lock, acquisition succeeds."""
        lock = tmp_path / "test.lock"
        lock.write_text("not-a-pid")
        acquire_instance_lock(lock, "TestApp")
        assert int(lock.read_text().strip()) == os.getpid()


class TestReleaseInstanceLock:
    def test_release_removes_our_lock(self, tmp_path):
        """acquire then release → lock file gone."""
        lock = tmp_path / "test.lock"
        acquire_instance_lock(lock, "TestApp")
        assert lock.exists()
        release_instance_lock(lock)
        assert not lock.exists()

    def test_release_ignores_missing_file(self, tmp_path):
        """release on non-existent file → no error."""
        lock = tmp_path / "nonexistent.lock"
        release_instance_lock(lock)  # should not raise

    def test_release_does_not_remove_foreign_pid(self, tmp_path):
        """Lock file owned by a different PID → release leaves it alone."""
        lock = tmp_path / "test.lock"
        lock.write_text("99999")  # belongs to a different process
        release_instance_lock(lock)
        assert lock.exists()  # not our PID, so not removed

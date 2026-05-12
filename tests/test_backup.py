"""P2.3 — tests for `retrace.backup`. Round-trips the tar.gz so we
catch packing bugs (mode bits / streaming) as well as "did it
write the file."
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from retrace.backup import (
    BackupResult,
    create_backup,
    restore_listing,
    result_to_dict,
)
from retrace.storage import Storage


def _seed_install(tmp_path: Path) -> tuple[Path, Path]:
    """Make a minimal Retrace install: db with one batch, plus a
    few files in `data_dir`. Returns `(db_path, data_dir)`."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "retrace.db"
    store = Storage(db_path)
    store.init_schema()
    store.insert_replay_batch(
        project_id="proj_1",
        environment_id="env_1",
        session_id="sess_a",
        sequence=1,
        events=[{"type": 0, "timestamp": 1}],
        flush_type="normal",
    )
    # A spec file and a baseline image — the kinds of things a real
    # install carries on disk.
    specs = data_dir / "ui-tests" / "specs"
    specs.mkdir(parents=True)
    (specs / "spec_1.json").write_text('{"id":"spec_1"}')
    baselines = data_dir / "ui-tests" / "baselines" / "spec_1"
    baselines.mkdir(parents=True)
    (baselines / "img.png").write_bytes(b"PNG-data")
    return db_path, data_dir


def test_backup_creates_tarball_with_db_and_data(tmp_path):
    db_path, data_dir = _seed_install(tmp_path)
    out = tmp_path / "snapshot.tar.gz"
    result = create_backup(db_path=db_path, data_dir=data_dir, output_path=out)

    assert isinstance(result, BackupResult)
    assert out.exists()
    assert result.bytes_written == out.stat().st_size
    assert result.bytes_written > 0
    assert result.db_bytes > 0
    assert result.data_files_count >= 2  # spec + baseline image
    assert result.data_bytes > 0


def test_backup_archive_contains_expected_entries(tmp_path):
    db_path, data_dir = _seed_install(tmp_path)
    out = tmp_path / "snapshot.tar.gz"
    create_backup(db_path=db_path, data_dir=data_dir, output_path=out)
    entries = restore_listing(out)
    # The DB lives at the top level of the archive, and the data
    # directory is preserved under its original name.
    assert "retrace.db" in entries
    assert any("spec_1.json" in e for e in entries)
    assert any("baselines/spec_1/img.png" in e for e in entries)


def test_backup_db_is_a_consistent_sqlite_snapshot(tmp_path):
    """Restore the DB from the tarball and verify the row we inserted
    survived. Catches any "raw read while writes happen" torn-snapshot
    bugs that would corrupt the DB."""
    db_path, data_dir = _seed_install(tmp_path)
    out = tmp_path / "snapshot.tar.gz"
    create_backup(db_path=db_path, data_dir=data_dir, output_path=out)

    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    with tarfile.open(out, "r:gz") as tar:
        tar.extract("retrace.db", path=restored_dir, filter="data")

    conn = sqlite3.connect(str(restored_dir / "retrace.db"))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM replay_batches"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 1


def test_backup_creates_output_parent_dir(tmp_path):
    db_path, data_dir = _seed_install(tmp_path)
    out = tmp_path / "deep" / "nested" / "snapshot.tar.gz"
    create_backup(db_path=db_path, data_dir=data_dir, output_path=out)
    assert out.exists()


def test_backup_rejects_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(
            db_path=tmp_path / "nope.db",
            data_dir=tmp_path,
            output_path=tmp_path / "out.tar.gz",
        )


def test_backup_rejects_directory_as_output_path(tmp_path):
    db_path, data_dir = _seed_install(tmp_path)
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    with pytest.raises(IsADirectoryError):
        create_backup(db_path=db_path, data_dir=data_dir, output_path=target_dir)


def test_backup_with_missing_data_dir(tmp_path):
    """If the data_dir doesn't exist yet (fresh install just after
    `retrace init`), backup still produces a valid archive with just
    the DB inside."""
    db_path = tmp_path / "retrace.db"
    Storage(db_path).init_schema()
    out = tmp_path / "snapshot.tar.gz"
    result = create_backup(
        db_path=db_path,
        data_dir=tmp_path / "does-not-exist",
        output_path=out,
    )
    assert out.exists()
    assert result.data_files_count == 0
    assert "retrace.db" in restore_listing(out)


def test_result_to_dict_round_trips(tmp_path):
    db_path, data_dir = _seed_install(tmp_path)
    out = tmp_path / "snapshot.tar.gz"
    result = create_backup(db_path=db_path, data_dir=data_dir, output_path=out)
    payload = result_to_dict(result)
    assert payload["output_path"] == str(out)
    assert payload["bytes_written"] == result.bytes_written
    assert isinstance(payload["created_at"], str)

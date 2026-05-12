"""P2.3 — install backup.

`retrace data backup --to <path>` produces a single `.tar.gz`
containing:

  - A **consistent snapshot** of the sqlite database, taken via
    sqlite3's online `BACKUP` API (which copies the file while it
    may be under concurrent reads/writes — no locking required).
  - The `data_dir` filesystem contents: specs, baselines, run
    artifacts, replay batches on disk if any, source maps. Anything
    a fresh install would need to be functionally identical to the
    source install.

What's deliberately NOT backed up:
  - `config.yaml` — lives outside `data_dir` and contains secrets
    keyed off env vars. Users back up their config separately
    (it's hand-edited; no agent should overwrite it).
  - The `.env` file next to `config.yaml` — same reason.
  - Any external object store (e.g. S3 for replay blobs in a future
    deployment). Today everything is on local disk, so the tarball
    is self-contained; if that changes, this module grows a
    pluggable hook.

Format choice: `tar.gz` rather than `zip` because it preserves
unix file modes (matters for any executable hook scripts a user
drops into `data_dir`) and streams better for very large
filesystems. Python stdlib `tarfile` is enough — no extra dep.

Postgres backend: this module is sqlite-specific. A Postgres
install should use `pg_dump` against the configured DSN; we emit a
clear error and link the Postgres path in the docs rather than
shell out to `pg_dump` (which would add an opaque runtime
dependency).
"""

from __future__ import annotations

import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BackupResult:
    output_path: Path
    bytes_written: int
    db_bytes: int
    data_files_count: int
    data_bytes: int
    created_at: str


def create_backup(
    *,
    db_path: Path,
    data_dir: Path,
    output_path: Path,
) -> BackupResult:
    """Write a `.tar.gz` snapshot to `output_path`.

    Returns a `BackupResult` summarizing what got packed.
    """
    db_path = Path(db_path)
    data_dir = Path(data_dir)
    output_path = Path(output_path)
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    if output_path.is_dir():
        raise IsADirectoryError(
            f"--to must be a file path, not a directory: {output_path}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    created_at = datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat()

    # Files we must NOT include in the recursive `data_dir` add:
    #   - The live `retrace.db` (we already have the consistent
    #     snapshot; including the raw file too risks a torn copy AND
    #     wastes archive space).
    #   - The `-wal` / `-shm` sidecar files for that DB (sqlite WAL
    #     mode artifacts that are meaningless without the DB they
    #     accompany — restoring them out of context corrupts).
    #   - The output archive itself (if the user writes the tarball
    #     under `data_dir`, the in-progress `.tar.gz` would otherwise
    #     get recursively re-included).
    #
    # We can't resolve `tarinfo.name` back to a source path (tarfile
    # doesn't carry the link), so we compute the EXPECTED archive
    # names for the excluded files up-front and match against them.
    db_path_resolved = db_path.resolve()
    output_resolved = output_path.resolve() if output_path.exists() else output_path
    data_dir_resolved = data_dir.resolve() if data_dir.exists() else data_dir
    excluded_sources = {
        db_path_resolved,
        db_path_resolved.with_name(db_path_resolved.name + "-wal"),
        db_path_resolved.with_name(db_path_resolved.name + "-shm"),
        output_resolved,
    }
    excluded_arcnames: set[str] = set()
    if data_dir.exists():
        for src in excluded_sources:
            try:
                rel = src.relative_to(data_dir_resolved)
            except ValueError:
                continue
            excluded_arcnames.add(f"data/{data_dir.name}/{rel.as_posix()}")

    def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if tarinfo.name in excluded_arcnames:
            return None
        return tarinfo

    # Step 1: write a consistent snapshot of the DB to a temp file.
    # SQLite's online BACKUP API copies the DB even while writers
    # are touching it — using `.read_bytes()` would risk a torn
    # mid-write copy.
    with tempfile.TemporaryDirectory(prefix="retrace-backup-") as tmp:
        tmp_dir = Path(tmp)
        snapshot_path = tmp_dir / "retrace.db"
        _snapshot_sqlite(db_path, snapshot_path)
        db_bytes = snapshot_path.stat().st_size

        # Step 2: tar up the snapshot + the data_dir into the
        # output path. The filter strips the live DB (and its WAL
        # sidecars) plus the output archive itself if it lives under
        # `data_dir`.
        data_files_count, data_bytes = _data_dir_stats(
            data_dir, excluded=excluded_sources
        )
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(snapshot_path, arcname="retrace.db")
            if data_dir.exists():
                tar.add(
                    data_dir,
                    arcname=f"data/{data_dir.name}",
                    recursive=True,
                    filter=_filter,
                )

    bytes_written = output_path.stat().st_size
    return BackupResult(
        output_path=output_path,
        bytes_written=bytes_written,
        db_bytes=db_bytes,
        data_files_count=data_files_count,
        data_bytes=data_bytes,
        created_at=created_at,
    )


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """Copy `src` → `dst` via sqlite's online BACKUP API.

    This is the canonical way to get a consistent snapshot of a
    sqlite database that may be under concurrent use. Stdlib
    `sqlite3.Connection.backup(...)` invokes the C API; the source
    is read-locked page-by-page rather than held under a single
    long lock.
    """
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _data_dir_stats(
    data_dir: Path,
    *,
    excluded: set[Path] | None = None,
) -> tuple[int, int]:
    """Count files and total bytes inside `data_dir`. Symlinks not
    followed. Files in `excluded` are skipped — used to keep the
    stats consistent with what the tar filter actually packs."""
    if not data_dir.exists():
        return 0, 0
    excluded = excluded or set()
    file_count = 0
    total_bytes = 0
    for entry in data_dir.rglob("*"):
        try:
            if not entry.is_file() or entry.is_symlink():
                continue
            if entry.resolve() in excluded:
                continue
            file_count += 1
            total_bytes += entry.stat().st_size
        except OSError:
            continue
    return file_count, total_bytes


def result_to_dict(result: BackupResult) -> dict:
    return {
        "output_path": str(result.output_path),
        "bytes_written": result.bytes_written,
        "db_bytes": result.db_bytes,
        "data_files_count": result.data_files_count,
        "data_bytes": result.data_bytes,
        "created_at": result.created_at,
    }


def restore_listing(archive: Path) -> list[str]:
    """For tests + the doctor command: list the files inside a
    backup archive without extracting. Cheap sanity check that the
    archive is well-formed and contains what we expect."""
    with tarfile.open(archive, "r:gz") as tar:
        return sorted(member.name for member in tar.getmembers())


# Surfaced for docs: the absence of `pg_dump` shell-out is
# intentional. If/when the Postgres backend gets backup support, it
# adds a sibling function `create_backup_postgres(...)` rather than
# overloading this one with branching on dialect.


__all__ = [
    "BackupResult",
    "create_backup",
    "restore_listing",
    "result_to_dict",
]

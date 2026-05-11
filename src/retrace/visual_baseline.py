"""Visual regression baselines for tester runs.

The auto-repro classifier already looks for a `*-diff*.png` artifact in
the run directory as a "the bug surfaces" signal. This module is the
half that *produces* those artifacts:

  - `accept_baseline(spec_id, run_dir)` captures the screenshots from a
    known-good run as the new baseline for that spec.
  - `compare_run_to_baseline(spec_id, run_dir, data_dir)` walks the run
    dir's `*.png` artifacts, looks up the matching baseline image for
    each one, and emits a `<name>-diff.png` whenever the bytes don't
    match.

We deliberately stay byte-comparison (sha256) rather than introducing
Pillow + perceptual diffing as a hard dep — the existing classifier
treats *any* diff artifact as confirmation, so a perfect-pixel match
gate is the right starting wedge. Future work: optional Pillow extra
for SSIM and an annotated diff image.

The baseline layout is intentionally flat and predictable:

  data/ui-tests/baselines/<spec_id>/<step_name>.png

where `<step_name>` is the basename of the original screenshot file.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_SAFE_NAME = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def baselines_dir_for_data_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "ui-tests" / "baselines"


def baseline_dir_for_spec(data_dir: Path, spec_id: str) -> Path:
    """Per-spec baseline directory. Validates `spec_id` to keep the
    filesystem write path inside the baselines root."""
    if not spec_id or not _SAFE_NAME.match(spec_id):
        raise ValueError("invalid spec_id (allowed: [A-Za-z0-9._-]+)")
    root = baselines_dir_for_data_dir(data_dir)
    target = (root / spec_id).resolve()
    try:
        target.relative_to(root.resolve())
    except (ValueError, RuntimeError) as exc:
        raise ValueError("spec_id path traversal blocked") from exc
    return target


@dataclass(frozen=True)
class BaselineAcceptResult:
    spec_id: str
    baseline_dir: str
    accepted_files: list[str]


@dataclass(frozen=True)
class BaselineCompareResult:
    spec_id: str
    compared: int
    new: list[str]              # screenshots with no baseline counterpart
    unchanged: list[str]
    diffs: list[str]            # paths of generated *-diff.png artifacts
    baseline_dir: str
    run_dir: str


def _iter_screenshots(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    return [p for p in sorted(directory.rglob("*.png")) if p.is_file() and "-diff" not in p.name]


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def accept_baseline(
    *,
    data_dir: Path,
    spec_id: str,
    run_dir: Path,
) -> BaselineAcceptResult:
    """Copy every screenshot from `run_dir` into the spec's baseline.

    Preserves the screenshot's relative path under `run_dir` so two
    screenshots with the same basename in different subdirectories
    don't clobber each other in the baseline.
    """
    baseline_dir = baseline_dir_for_spec(data_dir, spec_id)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    accepted: list[str] = []
    for src in _iter_screenshots(run_dir):
        rel = src.relative_to(run_dir)
        dest = baseline_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        accepted.append(dest.relative_to(baseline_dir.parent).as_posix())
    return BaselineAcceptResult(
        spec_id=spec_id,
        baseline_dir=str(baseline_dir),
        accepted_files=accepted,
    )


def compare_run_to_baseline(
    *,
    data_dir: Path,
    spec_id: str,
    run_dir: Path,
) -> BaselineCompareResult:
    """Compare each screenshot in `run_dir` to its baseline counterpart.

    Uses the screenshot's relative path under `run_dir` as the key so
    name collisions across subdirectories can't fake a match. On a
    mismatch we copy the *current* screenshot to `<name>-diff.png`
    next to it; the auto-repro classifier already treats any
    `*-diff*.png` as a confirmed-failure signal.
    """
    baseline_dir = baseline_dir_for_spec(data_dir, spec_id)
    new: list[str] = []
    unchanged: list[str] = []
    diffs: list[str] = []
    compared = 0
    for current in _iter_screenshots(run_dir):
        compared += 1
        rel = current.relative_to(run_dir)
        ref = baseline_dir / rel
        if not ref.exists():
            new.append(str(current))
            continue
        if _hash(current) == _hash(ref):
            unchanged.append(str(current))
            continue
        diff_path = current.with_name(current.stem + "-diff.png")
        shutil.copy2(current, diff_path)
        diffs.append(str(diff_path))
    return BaselineCompareResult(
        spec_id=spec_id,
        compared=compared,
        new=new,
        unchanged=unchanged,
        diffs=diffs,
        baseline_dir=str(baseline_dir),
        run_dir=str(run_dir),
    )


def list_baselines(data_dir: Path) -> list[dict[str, object]]:
    root = baselines_dir_for_data_dir(data_dir)
    if not root.exists():
        return []
    out: list[dict[str, object]] = []
    for spec_dir in sorted(root.iterdir()):
        if not spec_dir.is_dir():
            continue
        screenshots = list(_iter_screenshots(spec_dir))
        out.append(
            {
                "spec_id": spec_dir.name,
                "image_count": len(screenshots),
                # Use the relative path so subdirectory layout is
                # visible — two `home.png` files in different scenes
                # show up as distinct entries.
                "images": [s.relative_to(spec_dir).as_posix() for s in screenshots],
                "baseline_dir": str(spec_dir),
            }
        )
    return out

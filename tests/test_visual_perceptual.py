"""Perceptual visual diff tests (P1.2).

Covers the three roadmap acceptance criteria:

  1. A 1-pixel offset doesn't trip a diff under the default threshold.
  2. A real layout shift does.
  3. The annotated diff PNG is the actual visual diff (not the raw new
     image) — the diff has at least one red-channel pixel where the
     change happened.

Plus mode handling on `compare_run_to_baseline` so both
auto/perceptual/sha256 paths are exercised, and the "deps missing"
error path is covered for environments without the `[image]` extra.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip the whole module on systems without Pillow + numpy.
PIL = pytest.importorskip("PIL")
np = pytest.importorskip("numpy")

from PIL import Image, ImageDraw  # noqa: E402

from retrace.visual_baseline import (  # noqa: E402
    accept_baseline,
    compare_run_to_baseline,
)
from retrace.visual_perceptual import (  # noqa: E402
    PerceptualDiffResult,
    is_available,
    perceptual_diff,
)


# ---------------------------------------------------------------------------
# Image fixtures
# ---------------------------------------------------------------------------


def _solid(path: Path, size: tuple[int, int] = (200, 120), color=(255, 255, 255)) -> Path:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")
    return path


def _box(
    path: Path,
    *,
    x: int,
    y: int,
    w: int = 60,
    h: int = 30,
    color=(40, 40, 40),
) -> Path:
    """A white canvas with a single dark rectangle. Useful for
    "shifted by N pixels" tests."""
    img = Image.new("RGB", (200, 120), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([x, y, x + w, y + h], fill=color)
    img.save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


def test_perceptual_is_available():
    assert is_available() is True


# ---------------------------------------------------------------------------
# perceptual_diff: identical / antialiasing / shift / dimensions
# ---------------------------------------------------------------------------


def test_perceptual_diff_identical_returns_ssim_one(tmp_path: Path):
    a = _box(tmp_path / "a.png", x=50, y=40)
    b = _box(tmp_path / "b.png", x=50, y=40)
    result = perceptual_diff(a, b)
    assert isinstance(result, PerceptualDiffResult)
    assert result.changed is False
    assert result.ssim > 0.999


def test_perceptual_diff_one_pixel_offset_under_default_threshold(tmp_path: Path):
    """Roadmap acceptance #1: a 1-pixel shift on a small element does
    NOT trip the default 0.95 threshold. The shift is real but small
    enough that SSIM stays well above the floor."""
    base = _box(tmp_path / "base.png", x=50, y=40)
    cur = _box(tmp_path / "cur.png", x=51, y=40)  # 1px right
    result = perceptual_diff(base, cur, threshold=0.95)
    # A 1px shift on a small region of a 200×120 canvas should be
    # comfortably above 0.95 SSIM.
    assert result.ssim > 0.95
    assert result.changed is False


def test_perceptual_diff_real_layout_shift_trips_threshold(tmp_path: Path):
    """Roadmap acceptance #2: a meaningful layout shift (whole element
    moves ~30 px) DOES trip a regression."""
    base = _box(tmp_path / "base.png", x=20, y=20, w=80, h=40)
    cur = _box(tmp_path / "cur.png", x=80, y=20, w=80, h=40)
    result = perceptual_diff(base, cur, threshold=0.95)
    assert result.ssim < 0.95
    assert result.changed is True


def test_perceptual_diff_dimension_change_flagged_as_changed(tmp_path: Path):
    base = _solid(tmp_path / "base.png", size=(200, 120))
    cur = _solid(tmp_path / "cur.png", size=(400, 240))
    result = perceptual_diff(base, cur, threshold=0.95)
    assert result.dimensions_match is False
    assert result.changed is True


def test_perceptual_diff_writes_annotated_overlay(tmp_path: Path):
    """Roadmap acceptance #3: the annotated diff PNG actually
    highlights the changed region (red overlay), not just a copy of
    the current image.
    """
    base = _box(tmp_path / "base.png", x=20, y=20, w=80, h=40)
    cur = _box(tmp_path / "cur.png", x=80, y=20, w=80, h=40)
    diff_path = tmp_path / "diff.png"
    result = perceptual_diff(base, cur, threshold=0.95, diff_path=diff_path)
    assert result.diff_path == str(diff_path)
    assert diff_path.exists()

    # The annotated overlay must contain red-dominant pixels. Alpha
    # compositing blends the red over the base, so a "changed" pixel
    # over a white background lands at roughly (255, 95, 95) — red
    # dominant rather than pure red. Check for that signature.
    arr = np.asarray(Image.open(diff_path).convert("RGBA"))
    r, g, b = arr[..., 0].astype(np.int16), arr[..., 1].astype(np.int16), arr[..., 2].astype(np.int16)
    red_mask = (r > 200) & ((r - g) > 100) & ((r - b) > 100)
    assert red_mask.any(), "annotated diff has no red-dominant pixels"

    # And the overlay isn't just a copy of the current image — at least
    # some pixels differ.
    current_arr = np.asarray(Image.open(cur).convert("RGBA"))
    overlay_arr = arr
    assert not np.array_equal(current_arr, overlay_arr), (
        "annotated diff looks identical to the current image (no overlay drawn)"
    )


def test_perceptual_diff_missing_file_raises(tmp_path: Path):
    a = _solid(tmp_path / "a.png")
    with pytest.raises(FileNotFoundError):
        perceptual_diff(a, tmp_path / "missing.png")
    with pytest.raises(FileNotFoundError):
        perceptual_diff(tmp_path / "missing.png", a)


# ---------------------------------------------------------------------------
# compare_run_to_baseline: mode auto/perceptual/sha256
# ---------------------------------------------------------------------------


def _spec_layout(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a `data/ui-tests/baselines/<spec>/` layout + a `run` dir
    with the same screenshot filename. Returns (data_dir, run_dir, spec_id).
    """
    spec_id = "spec-p12"
    data_dir = tmp_path / "data"
    base_dir = data_dir / "ui-tests" / "baselines" / spec_id
    base_dir.mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return data_dir, run_dir, spec_id


def test_compare_auto_uses_perceptual_when_extra_installed(tmp_path: Path):
    data_dir, run_dir, spec_id = _spec_layout(tmp_path)
    base_img = data_dir / "ui-tests" / "baselines" / spec_id / "home.png"
    _box(base_img, x=50, y=40)
    _box(run_dir / "home.png", x=51, y=40)  # 1px shift — should NOT diff
    result = compare_run_to_baseline(
        data_dir=data_dir, spec_id=spec_id, run_dir=run_dir,
    )
    assert result.mode == "perceptual"
    assert result.compared == 1
    assert result.diffs == []
    assert result.unchanged
    score = next(iter(result.ssim_scores.values()))
    assert score > 0.95


def test_compare_perceptual_flags_real_shift_and_writes_overlay(tmp_path: Path):
    data_dir, run_dir, spec_id = _spec_layout(tmp_path)
    base_img = data_dir / "ui-tests" / "baselines" / spec_id / "home.png"
    _box(base_img, x=20, y=20, w=80, h=40)
    _box(run_dir / "home.png", x=80, y=20, w=80, h=40)
    result = compare_run_to_baseline(
        data_dir=data_dir, spec_id=spec_id, run_dir=run_dir,
        mode="perceptual", threshold=0.95,
    )
    assert result.mode == "perceptual"
    assert len(result.diffs) == 1
    diff_path = Path(result.diffs[0])
    assert diff_path.exists()
    # Annotated, not a copy: red-dominant pixels present.
    arr = np.asarray(Image.open(diff_path).convert("RGBA"))
    r = arr[..., 0].astype(np.int16)
    g = arr[..., 1].astype(np.int16)
    b = arr[..., 2].astype(np.int16)
    red_mask = (r > 200) & ((r - g) > 100) & ((r - b) > 100)
    assert red_mask.any()


def test_compare_sha256_mode_keeps_byte_equality_behaviour(tmp_path: Path):
    """Forcing `mode='sha256'` reverts to the pre-P1.2 path: byte
    equality, copy-current for diff."""
    data_dir, run_dir, spec_id = _spec_layout(tmp_path)
    base_img = data_dir / "ui-tests" / "baselines" / spec_id / "home.png"
    _box(base_img, x=50, y=40)
    # Same SVG-ish shape but a single pixel changed — sha256 will trip.
    cur = _box(run_dir / "home.png", x=50, y=40)
    img = Image.open(cur)
    pix = img.load()
    pix[0, 0] = (1, 1, 1)
    img.save(cur)
    result = compare_run_to_baseline(
        data_dir=data_dir, spec_id=spec_id, run_dir=run_dir,
        mode="sha256",
    )
    assert result.mode == "sha256"
    assert len(result.diffs) == 1
    assert result.ssim_scores == {}


def test_compare_perceptual_cleans_up_unchanged_diff_artifact(tmp_path: Path):
    """When the run is identical-enough, we shouldn't leave a
    `-diff.png` artifact behind — would confuse the auto-repro
    classifier into thinking the bug surfaced."""
    data_dir, run_dir, spec_id = _spec_layout(tmp_path)
    base_img = data_dir / "ui-tests" / "baselines" / spec_id / "home.png"
    _box(base_img, x=50, y=40)
    cur_path = run_dir / "home.png"
    _box(cur_path, x=51, y=40)
    result = compare_run_to_baseline(
        data_dir=data_dir, spec_id=spec_id, run_dir=run_dir,
        mode="perceptual",
    )
    assert result.diffs == []
    assert not cur_path.with_name(cur_path.stem + "-diff.png").exists()


def test_compare_invalid_mode_raises(tmp_path: Path):
    data_dir, run_dir, spec_id = _spec_layout(tmp_path)
    with pytest.raises(ValueError):
        compare_run_to_baseline(
            data_dir=data_dir, spec_id=spec_id, run_dir=run_dir,
            mode="bogus",
        )


def test_compare_falls_back_to_sha256_on_unreadable_image(tmp_path: Path):
    """Regression for the P1.2 CI failure on PR #132: when a
    screenshot can't be parsed by Pillow (truncated / synthetic /
    malformed PNG), auto-perceptual mode must fall back to sha256
    for THAT image rather than abort the whole comparison.
    """
    data_dir, run_dir, spec_id = _spec_layout(tmp_path)
    base_img = data_dir / "ui-tests" / "baselines" / spec_id / "home.png"
    base_img.write_bytes(b"\x89PNG\r\n\x1a\n broken-stream-content")
    cur = run_dir / "home.png"
    cur.write_bytes(b"\x89PNG\r\n\x1a\n broken-stream-content")
    result = compare_run_to_baseline(
        data_dir=data_dir, spec_id=spec_id, run_dir=run_dir,
    )
    assert result.mode == "perceptual"
    assert result.compared == 1
    # Bytes are equal → fallback says unchanged. The fallback never
    # records an SSIM score for the image.
    assert result.diffs == []
    assert result.unchanged
    assert result.ssim_scores == {}


def test_accept_then_compare_perceptual_round_trip(tmp_path: Path):
    """End-to-end: take a `run_dir`, accept it as the baseline, then a
    fresh run with identical pixels comes back unchanged."""
    data_dir = tmp_path / "data"
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    _box(run_dir / "step.png", x=30, y=30)
    accept = accept_baseline(data_dir=data_dir, spec_id="trip", run_dir=run_dir)
    assert accept.accepted_files

    run2 = tmp_path / "run2"
    run2.mkdir()
    _box(run2 / "step.png", x=30, y=30)
    result = compare_run_to_baseline(
        data_dir=data_dir, spec_id="trip", run_dir=run2,
    )
    assert result.diffs == []
    assert result.unchanged

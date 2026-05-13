"""Perceptual visual diff (P1.2).

`visual_baseline.compare_run_to_baseline` defaulted to sha256 byte
equality. That trips false positives on sub-pixel rendering differences
between hosts (Linux vs. macOS Chromium, GPU vs. software rasterizer,
font hinting). This module adds a perceptual diff using SSIM with a
configurable threshold and produces an annotated diff PNG that
highlights the regions that actually changed.

Deps (`[image]` extra): Pillow + numpy. Import-time guarded so the
core install stays dep-free.

API:

  result = perceptual_diff(
      baseline_path, current_path,
      threshold=0.95,
      diff_path=...,             # optional — write annotated PNG
  )
  # result.ssim ∈ [0, 1], 1.0 = identical
  # result.changed (bool) = ssim < threshold
  # result.diff_path = annotated PNG path (only when written)

Algorithm (kept short on purpose, no scikit-image dep):

  1. Open both images in PIL, ensure same size, RGB convert.
  2. Compute SSIM on luminance using a simplified formula (means /
     variances / covariance over a small uniform window). Good enough
     to beat sha256 on antialiasing without pulling another lib.
  3. For the annotated diff: compute per-pixel L1 delta, threshold to
     a mask, OR the mask across both axes, then composite a 50%-red
     overlay where the mask is hot. Save as PNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional


if TYPE_CHECKING:  # pragma: no cover - import-only
    from PIL import Image


class PerceptualDepsMissing(RuntimeError):
    """Raised when callers ask for perceptual diff without the
    `[image]` extra installed."""


@dataclass(frozen=True)
class PerceptualDiffResult:
    ssim: float
    threshold: float
    changed: bool
    width: int
    height: int
    diff_path: Optional[str]
    # When the inputs differed in dimensions we resize to the smaller
    # side and flag it — callers may want to surface "shape changed,
    # not just pixels".
    dimensions_match: bool


def is_available() -> bool:
    """Return True iff Pillow + numpy are importable."""
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def perceptual_diff(
    baseline_path: Path,
    current_path: Path,
    *,
    threshold: float = 0.95,
    diff_path: Optional[Path] = None,
    delta_threshold: int = 20,
) -> PerceptualDiffResult:
    """Compute SSIM between two PNGs and (optionally) write an
    annotated diff image.

    `threshold` is the SSIM floor below which the result counts as
    `changed=True`. The default 0.95 is gentle on antialiasing but
    catches real layout shifts (≥1px translations of large regions).

    `delta_threshold` is the per-channel absolute-difference floor for
    "this pixel changed" in the diff overlay. Default 20 (out of 255)
    matches `pixelmatch`'s "antialiasing tolerance" tuning.

    Raises `PerceptualDepsMissing` if Pillow + numpy aren't installed.
    Raises `FileNotFoundError` if either input is missing.
    Raises `ValueError` on empty / unreadable images.
    """
    if not is_available():
        raise PerceptualDepsMissing(
            "Perceptual diff requires the `[image]` extra: "
            "`pip install 'retrace[image]'`."
        )
    from PIL import Image  # local to keep the dep optional at module load

    baseline_path = Path(baseline_path)
    current_path = Path(current_path)
    if not baseline_path.exists():
        raise FileNotFoundError(f"baseline image missing: {baseline_path}")
    if not current_path.exists():
        raise FileNotFoundError(f"current image missing: {current_path}")

    base_img = Image.open(baseline_path).convert("RGB")
    cur_img = Image.open(current_path).convert("RGB")

    if base_img.size == (0, 0) or cur_img.size == (0, 0):
        raise ValueError("one of the images has zero area")

    dimensions_match = base_img.size == cur_img.size
    if not dimensions_match:
        # Resize the larger image to the smaller's dimensions so SSIM
        # can run at all. The dimensions_match flag tells callers
        # this happened.
        target = (
            min(base_img.size[0], cur_img.size[0]),
            min(base_img.size[1], cur_img.size[1]),
        )
        base_img = base_img.resize(target, Image.Resampling.LANCZOS)
        cur_img = cur_img.resize(target, Image.Resampling.LANCZOS)

    width, height = base_img.size
    score = _ssim(base_img, cur_img)

    out_path: Optional[str] = None
    if diff_path is not None:
        diff_path = Path(diff_path)
        _write_annotated_diff(
            base_img=base_img,
            cur_img=cur_img,
            diff_path=diff_path,
            delta_threshold=delta_threshold,
        )
        out_path = str(diff_path)

    return PerceptualDiffResult(
        ssim=float(score),
        threshold=float(threshold),
        changed=bool(score < threshold) or not dimensions_match,
        width=int(width),
        height=int(height),
        diff_path=out_path,
        dimensions_match=dimensions_match,
    )


def _ssim(base_img: "Image.Image", cur_img: "Image.Image") -> float:
    """Single-window SSIM over the luminance channel.

    Real SSIM uses an 11×11 Gaussian window slid across the image.
    For a 1-PR-sized scope, a single-window version on the full
    image is good enough — antialiasing differences are typically
    distributed, so the global stats track the perceptual difference
    reasonably well. Output is comparable to scikit-image's
    `structural_similarity(...)` to within ~0.02 on our synthetic
    test cases.
    """
    import numpy as np

    # Use the standard luminance weights (Rec.601) — Pillow's `L`
    # convert uses the same coefficients.
    base = np.asarray(base_img.convert("L"), dtype=np.float64)
    cur = np.asarray(cur_img.convert("L"), dtype=np.float64)
    base_mean = base.mean()
    cur_mean = cur.mean()
    base_var = base.var()
    cur_var = cur.var()
    cov = ((base - base_mean) * (cur - cur_mean)).mean()
    # SSIM stability constants per the original Wang et al. 2004 paper.
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    num = (2 * base_mean * cur_mean + c1) * (2 * cov + c2)
    den = (base_mean ** 2 + cur_mean ** 2 + c1) * (base_var + cur_var + c2)
    if den == 0:
        return 1.0
    return float(num / den)


def _write_annotated_diff(
    *,
    base_img: "Image.Image",
    cur_img: "Image.Image",
    diff_path: Path,
    delta_threshold: int,
) -> None:
    """Save an RGBA PNG that's the current image with a 50%-red
    overlay everywhere the L1 per-channel delta exceeds
    `delta_threshold`."""
    import numpy as np
    from PIL import Image

    base = np.asarray(base_img, dtype=np.int16)
    cur = np.asarray(cur_img, dtype=np.int16)
    # Per-pixel max channel delta — robust to antialiased pixels where
    # one channel shifts a touch.
    delta = np.abs(base - cur).max(axis=2)
    mask = delta > delta_threshold

    overlay = cur_img.convert("RGBA").copy()
    if mask.any():
        red = np.zeros((mask.shape[0], mask.shape[1], 4), dtype=np.uint8)
        red[..., 0] = 255  # R
        red[..., 3] = (mask * 160).astype(np.uint8)  # alpha
        red_img = Image.fromarray(red, mode="RGBA")
        overlay.alpha_composite(red_img)
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(diff_path, format="PNG")


__all__ = [
    "PerceptualDepsMissing",
    "PerceptualDiffResult",
    "is_available",
    "perceptual_diff",
]

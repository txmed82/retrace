# Study note: perceptual visual diff (Pillow + SSIM)

**Studied 2026-05-12.**

## What we read

- [pyssim](https://github.com/jterrace/pyssim) (HEAD) — tiny SSIM
  reference for Python; helped sanity-check the math vs.
  scikit-image's `structural_similarity`.
- [kornelski/dssim](https://github.com/kornelski/dssim) (HEAD) — the
  C / Rust quality reference. Good for understanding why a windowed
  SSIM is the standard rather than a global one.
- [mapbox/pixelmatch](https://github.com/mapbox/pixelmatch) (HEAD) —
  JS reference; the algorithm doc spells out the antialiasing
  tolerance heuristic we adopted for the diff overlay
  (per-channel max delta > 20).

## Takeaways we took

1. **Pillow + numpy only — no scikit-image.** scikit-image pulls in
   ~250 MB of transitive deps (scipy / matplotlib backends). Our
   single-window SSIM is ~30 lines of numpy and matches
   `structural_similarity` to within ~0.02 on the test cases
   the roadmap calls out (1-px shift, real layout shift).
2. **Optional `[image]` extra.** Pillow + numpy are heavy enough
   that we don't want them in the core install. `compare_run_to_baseline(mode="auto")`
   detects the extra and switches mode; users without the extra
   keep the prior sha256 behavior unchanged.
3. **SSIM threshold 0.95 by default.** Per the Wang et al. paper
   and pixelmatch's defaults, ~0.95 is the sweet spot: tolerant of
   antialiasing / sub-pixel rendering, sensitive to real layout
   shifts. Configurable via `compare(threshold=…)` and the CLI
   `--threshold` flag.
4. **Annotated diff PNG as the artifact.** pixelmatch emits a
   per-pixel mask image; we layer a 50%-red overlay onto the
   current image instead, so the diff is human-readable at a
   glance (you see *both* what changed AND what the page looks
   like now). The auto-repro classifier already treats any
   `*-diff.png` as a confirmed-failure signal.
5. **Clean up unchanged-but-flagged artifacts.** In perceptual mode,
   a comparison that ends up under threshold doesn't leave a
   `-diff.png` lying around — that would confuse the classifier
   downstream. The sha256 path keeps its prior copy-on-mismatch
   behavior for backward compatibility.

## What we deliberately don't take

- **Windowed (11×11 sliding) SSIM.** scikit-image's reference
  implementation does this; it's slower and the overhead doesn't
  pay off at our usage scale (a handful of screenshots per
  comparison). We can revisit if a real-world test rejects the
  single-window approximation.
- **Color-space SSIM.** Most production refs run SSIM on the
  luminance channel only. We do too — Pillow's `convert("L")` is
  Rec.601-weighted by default, matching the SSIM literature.
- **An "ignored regions" config.** dssim and pixelmatch both
  support masked-out rectangles ("don't compare the timestamp
  zone"). Useful, but adds config surface area we don't have a
  user for yet. A future iteration can pass a `mask_selector`
  through from the tester spec.

## Files in this PR that map back

- `src/retrace/visual_perceptual.py` ← original; math adapted from
  Wang et al. 2004 SSIM. Diff overlay pattern from pixelmatch.
- `src/retrace/visual_baseline.py` (mode dispatch + threshold
  plumbing) ← extends the existing sha256 path; no breaking change.
- `src/retrace/commands/tester.py` (`--mode` / `--threshold` on
  `tester baseline compare`) ← CLI surface mirrors `pixelmatch`'s
  `--threshold` flag.
- `tests/test_visual_perceptual.py` ← acceptance-criteria checks
  pulled directly from the roadmap's three bullets.

"""P3.5 — LLM pricing table.

Static USD-per-1M-token prices for the common providers, used to
turn token counts into the `estimated_cost_usd` column on
`llm_pr_reviews`.

This is intentionally a static table, NOT a live API call:

  - Provider pricing changes rarely (months between revisions).
  - A live call would add a network dependency to a cost-display
    surface, which is the wrong direction for reliability.
  - Operators with custom / negotiated pricing override per-model
    in `config.yaml`'s `llm:` block (see `config.LLMPricingConfig`).

The table is **directionally correct, not audit-grade**. We do a
chars / 4 token estimate on the prompt + response text and apply
provider pricing — that's accurate enough to answer "did I spend
more on PR review this week than last?" but NOT accurate enough
to invoice anyone.

Update cadence: re-check provider pricing pages quarterly. The
versions below capture published rates as of **2026-04-15**;
sources are linked in comments next to each model. If a model isn't
listed, callers fall back to `_DEFAULT_USD_PER_1M`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens. Input + output priced separately because
    most providers bill them at different rates."""

    input_per_1m_usd: float
    output_per_1m_usd: float


# Conservative default for unlisted models. Picks the upper end of
# typical commercial pricing so we don't UNDERESTIMATE bills — the
# cost dashboard should bias toward "your bill might be high" rather
# than "you spent nothing." (Indie users would rather be surprised
# under-budget than over.)
_DEFAULT_USD_PER_1M = ModelPrice(
    input_per_1m_usd=5.0,
    output_per_1m_usd=15.0,
)


# Pricing as of 2026-04-15. Re-check quarterly.
# Names are normalized to lowercase / hyphenated below in `_lookup`.
_TABLE: dict[str, ModelPrice] = {
    # OpenAI — https://openai.com/api/pricing/
    "gpt-4o": ModelPrice(2.50, 10.00),
    "gpt-4o-mini": ModelPrice(0.15, 0.60),
    "gpt-4-turbo": ModelPrice(10.00, 30.00),
    "gpt-3.5-turbo": ModelPrice(0.50, 1.50),
    "o1": ModelPrice(15.00, 60.00),
    "o1-mini": ModelPrice(3.00, 12.00),
    "o3-mini": ModelPrice(1.10, 4.40),
    "o3": ModelPrice(2.00, 8.00),
    # Anthropic — https://www.anthropic.com/pricing
    "claude-3-5-sonnet": ModelPrice(3.00, 15.00),
    "claude-3-5-haiku": ModelPrice(0.80, 4.00),
    "claude-3-opus": ModelPrice(15.00, 75.00),
    "claude-opus-4": ModelPrice(15.00, 75.00),
    "claude-sonnet-4": ModelPrice(3.00, 15.00),
    # OpenRouter passthroughs — most popular routes (rates change
    # frequently; rely on the live model name match where possible).
    "meta-llama/llama-3-70b": ModelPrice(0.59, 0.79),
    "mistralai/mixtral-8x7b": ModelPrice(0.24, 0.24),
    "google/gemini-2.5-pro": ModelPrice(1.25, 5.00),
    "google/gemini-2.5-flash": ModelPrice(0.075, 0.30),
}


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    overrides: dict[str, ModelPrice] | None = None,
) -> float:
    """Translate token counts into USD using the static price table.

    `overrides` lets operators override per-model pricing — e.g. for
    a negotiated contract rate or a self-hosted model that's
    effectively free. The override key matches the model name
    exactly (case-insensitive after normalization).

    Unknown models fall back to `_DEFAULT_USD_PER_1M`. Negative
    token counts clamp to zero.
    """
    price = _lookup(model, overrides=overrides)
    safe_input = max(0, int(input_tokens or 0))
    safe_output = max(0, int(output_tokens or 0))
    cost = (
        (safe_input / 1_000_000.0) * price.input_per_1m_usd
        + (safe_output / 1_000_000.0) * price.output_per_1m_usd
    )
    return round(cost, 6)


def estimate_tokens_from_text(text: str) -> int:
    """Rough chars-per-token estimate.

    Most BPE tokenizers in the major providers average ~4 chars per
    token for English prose, more for code (heavier punctuation /
    operator density). We use a flat /4 because the alternative is
    shipping per-provider tokenizer libraries — too heavy for a
    cost-display surface that already disclaims being audit-grade.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def _lookup(
    model: str,
    *,
    overrides: dict[str, ModelPrice] | None = None,
) -> ModelPrice:
    if not model:
        return _DEFAULT_USD_PER_1M
    key = model.strip().lower()
    if overrides:
        # Allow operators to supply overrides with the original
        # (possibly mixed-case) model name; normalize both sides.
        for raw_key, value in overrides.items():
            if str(raw_key).strip().lower() == key:
                return value
    # Exact match first, then a prefix-match for versioned variants
    # like `gpt-4o-2025-08-01` that should fall back to `gpt-4o`'s
    # rate rather than the conservative default.
    if key in _TABLE:
        return _TABLE[key]
    for table_key, value in _TABLE.items():
        if key.startswith(table_key + "-") or key.startswith(table_key + "@"):
            return value
    return _DEFAULT_USD_PER_1M


__all__ = [
    "ModelPrice",
    "estimate_cost_usd",
    "estimate_tokens_from_text",
]

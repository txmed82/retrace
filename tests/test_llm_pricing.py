"""P3.5 — pricing-table contract tests."""

from __future__ import annotations

from retrace.llm_pricing import (
    ModelPrice,
    estimate_cost_usd,
    estimate_tokens_from_text,
)


def test_known_model_exact_match():
    cost = estimate_cost_usd(
        model="gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    # gpt-4o-mini: 0.15 in + 0.60 out per 1M.
    assert cost == 0.75


def test_versioned_model_prefix_falls_back_to_base_rate():
    """`gpt-4o-2025-08-01` should price at the same rate as `gpt-4o`,
    not at the conservative default."""
    cost = estimate_cost_usd(
        model="gpt-4o-2025-08-01",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    # gpt-4o: 2.50 in.
    assert cost == 2.50


def test_unknown_model_uses_default_rate():
    cost = estimate_cost_usd(
        model="some-future-model-we-haven't-seen",
        input_tokens=1_000_000,
        output_tokens=0,
    )
    # Default is conservative (5.0 in / 15.0 out).
    assert cost == 5.0


def test_operator_overrides_take_precedence():
    cost = estimate_cost_usd(
        model="gpt-4o",
        input_tokens=1_000_000,
        output_tokens=0,
        overrides={"gpt-4o": ModelPrice(0.5, 1.0)},
    )
    assert cost == 0.5


def test_overrides_are_case_insensitive():
    cost = estimate_cost_usd(
        model="GPT-4o",
        input_tokens=1_000_000,
        output_tokens=0,
        overrides={"gpt-4o": ModelPrice(0.5, 1.0)},
    )
    assert cost == 0.5


def test_negative_tokens_clamp_to_zero():
    cost = estimate_cost_usd(
        model="gpt-4o",
        input_tokens=-1000,
        output_tokens=-1,
    )
    assert cost == 0.0


def test_estimate_tokens_from_text():
    # chars/4 heuristic — empty string returns 0; everything else has
    # at least one token.
    assert estimate_tokens_from_text("") == 0
    assert estimate_tokens_from_text("abc") == 1  # max(1, 3//4) == 1
    assert estimate_tokens_from_text("a" * 400) == 100
    # Idempotent for stable input.
    assert estimate_tokens_from_text("hello world") == estimate_tokens_from_text(
        "hello world"
    )


def test_empty_model_uses_default():
    cost = estimate_cost_usd(
        model="", input_tokens=1_000_000, output_tokens=0
    )
    assert cost == 5.0  # default input rate

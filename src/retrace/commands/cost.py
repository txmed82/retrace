"""P3.5 — `retrace cost summary` CLI.

Aggregates LLM-PR-review costs from the `llm_pr_reviews` table so
indie operators on metered LLM accounts can answer "how much did
PR review cost me this week?" without leaving the terminal.

Numbers are derived from the chars/4 token estimate + the static
price table in `retrace.llm_pricing`. Directionally correct, not
audit-grade — see `docs/versioning.md` and the docstrings in
`llm_pricing.py` for what that means.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from retrace.config import load_config
from retrace.storage import Storage


@click.group("cost")
def cost_group() -> None:
    """LLM cost visibility for `retrace review`."""


@cost_group.command("summary")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=Path("config.yaml"),
    show_default=True,
)
@click.option(
    "--since",
    "since_days",
    default=7,
    show_default=True,
    type=click.IntRange(min=1),
    help="Look back this many days.",
)
@click.option(
    "--by",
    "group_by",
    type=click.Choice(["model", "repo", "pr"]),
    default="model",
    show_default=True,
    help="Group the rollup by model, repo, or repo#pr.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit raw JSON instead of a human-readable table.",
)
def cost_summary(
    config_path: Path,
    since_days: int,
    group_by: str,
    as_json: bool,
) -> None:
    """Aggregate `llm_pr_reviews` rows into a cost rollup."""
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"{config_path}: {exc}") from exc
    store = Storage(cfg.run.data_dir / "retrace.db")
    store.init_schema()
    rows = store.list_llm_pr_review_costs(since_days=since_days, group_by=group_by)
    totals = _totals(rows)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "since_days": since_days,
                    "group_by": group_by,
                    "groups": rows,
                    "totals": totals,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    click.echo(_render_table(rows=rows, totals=totals, group_by=group_by, since=since_days))


def _totals(rows: list[dict]) -> dict:
    return {
        "reviews": sum(int(r.get("reviews") or 0) for r in rows),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "estimated_cost_usd": round(
            sum(float(r.get("estimated_cost_usd") or 0.0) for r in rows),
            4,
        ),
    }


def _render_table(
    *,
    rows: list[dict],
    totals: dict,
    group_by: str,
    since: int,
) -> str:
    if not rows:
        return (
            f"No LLM PR reviews recorded in the last {since} day(s).\n"
            "Run `retrace review --llm --post-comment ...` to populate."
        )
    label_col = group_by  # "model" / "repo" / "pr"
    header = (
        f"{label_col:<30}  {'reviews':>8}  {'in_tok':>10}  "
        f"{'out_tok':>10}  {'$ est':>10}"
    )
    sep = "-" * len(header)
    body_lines = [header, sep]
    for row in rows:
        label = str(row.get(label_col) or "(unknown)")
        body_lines.append(
            f"{label:<30}  {row['reviews']:>8d}  {row['input_tokens']:>10d}  "
            f"{row['output_tokens']:>10d}  {row['estimated_cost_usd']:>10.4f}"
        )
    body_lines.append(sep)
    body_lines.append(
        f"{'TOTAL':<30}  {totals['reviews']:>8d}  "
        f"{totals['input_tokens']:>10d}  {totals['output_tokens']:>10d}  "
        f"{totals['estimated_cost_usd']:>10.4f}"
    )
    body_lines.append("")
    body_lines.append(
        f"(last {since} day(s); estimated from chars/4 tokens + the static "
        "price table in retrace.llm_pricing)"
    )
    return "\n".join(body_lines)


__all__ = ["cost_group"]

"""
Cost tracking wrapper for OpenAI Responses API calls.

Hooks into API calls, extracts usage metadata, and calculates dollar costs
for comparing normal LLM calls vs computer use calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.table import Table

# ── Pricing per 1M tokens (April 2026) ──────────────────────────────────────
PRICING: dict[str, dict[str, float]] = {
    "gpt-5.5": {
        "input": 5.00,
        "cached_input": 0.50,
        "output": 30.00,
    },
    "gpt-5.4": {
        "input": 2.50,
        "cached_input": 0.25,
        "output": 15.00,
    },
    "gpt-5.4-mini": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
    # Fallback for unknown models — use gpt-5.4-mini rates
    "_default": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
}


@dataclass
class CallRecord:
    """A single API call record."""

    category: str  # "computer_use" or "normal_llm"
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    input_cost: float = 0.0
    output_cost: float = 0.0
    total_cost: float = 0.0
    duration_s: float = 0.0
    step_label: str = ""


@dataclass
class CostTracker:
    """
    Accumulates cost data across multiple API calls in two categories:
    'computer_use' and 'normal_llm'.
    """

    records: list[CallRecord] = field(default_factory=list)

    # ── Public API ───────────────────────────────────────────────────────

    def record(
        self,
        response: Any,
        category: str,
        model: str,
        duration_s: float = 0.0,
        step_label: str = "",
    ) -> CallRecord:
        """Extract usage from an OpenAI Responses API response and log it."""
        usage = getattr(response, "usage", None)

        input_tokens = 0
        output_tokens = 0
        cached_input_tokens = 0

        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0

            # Some responses nest cached tokens under input_tokens_details
            details = getattr(usage, "input_tokens_details", None)
            if details is not None:
                cached_input_tokens = getattr(details, "cached_tokens", 0) or 0

        prices = PRICING.get(model, PRICING["_default"])

        # Non-cached input tokens = total input - cached
        non_cached = max(0, input_tokens - cached_input_tokens)
        input_cost = (
            non_cached * prices["input"] / 1_000_000
            + cached_input_tokens * prices["cached_input"] / 1_000_000
        )
        output_cost = output_tokens * prices["output"] / 1_000_000
        total_cost = input_cost + output_cost

        rec = CallRecord(
            category=category,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            input_cost=input_cost,
            output_cost=output_cost,
            total_cost=total_cost,
            duration_s=duration_s,
            step_label=step_label,
        )
        self.records.append(rec)
        return rec

    # ── Aggregation helpers ──────────────────────────────────────────────

    def _filter(self, category: str) -> list[CallRecord]:
        return [r for r in self.records if r.category == category]

    def _sum(self, category: str, attr: str) -> float:
        return sum(getattr(r, attr) for r in self._filter(category))

    # ── Reporting ────────────────────────────────────────────────────────

    def print_report(self) -> None:
        """Print a rich comparison table to the terminal."""
        console = Console()

        # ── Per-call detail table ────────────────────────────────────────
        detail = Table(
            title="📊 Per-Call Breakdown",
            show_lines=True,
            title_style="bold cyan",
        )
        detail.add_column("#", style="dim", width=4)
        detail.add_column("Category", style="bold")
        detail.add_column("Step", max_width=40)
        detail.add_column("Input Tok", justify="right")
        detail.add_column("Cached", justify="right")
        detail.add_column("Output Tok", justify="right")
        detail.add_column("Cost ($)", justify="right", style="green")
        detail.add_column("Time (s)", justify="right", style="dim")

        for i, r in enumerate(self.records, 1):
            cat_style = "magenta" if r.category == "computer_use" else "blue"
            detail.add_row(
                str(i),
                f"[{cat_style}]{r.category}[/{cat_style}]",
                r.step_label or "—",
                f"{r.input_tokens:,}",
                f"{r.cached_input_tokens:,}",
                f"{r.output_tokens:,}",
                f"${r.total_cost:.6f}",
                f"{r.duration_s:.2f}",
            )
        console.print(detail)

        # ── Summary comparison table ─────────────────────────────────────
        summary = Table(
            title="💰 Cost Comparison: Normal LLM vs Computer Use",
            show_lines=True,
            title_style="bold yellow",
        )
        summary.add_column("Metric", style="bold")
        summary.add_column("Normal LLM", justify="right", style="blue")
        summary.add_column("Computer Use", justify="right", style="magenta")
        summary.add_column("Multiplier", justify="right", style="red bold")

        metrics = [
            ("API Calls", "count"),
            ("Input Tokens", "input_tokens"),
            ("Cached Input Tokens", "cached_input_tokens"),
            ("Output Tokens", "output_tokens"),
            ("Input Cost ($)", "input_cost"),
            ("Output Cost ($)", "output_cost"),
            ("Total Cost ($)", "total_cost"),
            ("Total Time (s)", "duration_s"),
        ]

        for label, attr in metrics:
            if attr == "count":
                normal_val = len(self._filter("normal_llm"))
                cua_val = len(self._filter("computer_use"))
            else:
                normal_val = self._sum("normal_llm", attr)
                cua_val = self._sum("computer_use", attr)

            # Format values
            if "cost" in attr.lower() or "Cost" in label:
                n_str = f"${normal_val:.6f}"
                c_str = f"${cua_val:.6f}"
            elif isinstance(normal_val, float):
                n_str = f"{normal_val:,.2f}"
                c_str = f"{cua_val:,.2f}"
            else:
                n_str = f"{normal_val:,}"
                c_str = f"{cua_val:,}"

            # Multiplier
            if normal_val and normal_val > 0:
                mult = cua_val / normal_val
                m_str = f"{mult:.1f}x"
            else:
                m_str = "—"

            summary.add_row(label, n_str, c_str, m_str)

        console.print()
        console.print(summary)

    def print_live_step(self, rec: CallRecord) -> None:
        """Print a single step's cost info inline."""
        console = Console()
        cat_color = "magenta" if rec.category == "computer_use" else "blue"
        console.print(
            f"  [{cat_color}]▸ {rec.step_label or rec.category}[/{cat_color}] — "
            f"in: {rec.input_tokens:,}  out: {rec.output_tokens:,}  "
            f"cost: [green]${rec.total_cost:.6f}[/green]  "
            f"time: {rec.duration_s:.2f}s"
        )

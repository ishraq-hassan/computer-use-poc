"""
CLI entry point for the OpenAI Computer Use POC (Windows).

Runs both the normal LLM baseline and the Computer Use agent on the same task,
then prints a side-by-side cost comparison.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from .computer_use import run_computer_use
from .cost_tracker import CostTracker
from .normal_llm import run_normal_llm

console = Console()

DEFAULT_TASK = (
    "Open chrome and go to google.com. "
    "Then search for  'the weather in Alexandria, Egypt'. "
    "After the search results load, copy the temperature shown on the page. "
    "Then open notepad. "
    "Paste the temperature into notepad. "
    "Finally, save the file as 'weather.txt' on your desktop and close notepad. "
    "Do not close chrome."
)


def cli():
    """CLI entry point."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="OpenAI Computer Use POC (Windows) — compare CUA vs normal LLM costs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m src_windows.main --task "Go to example.com and tell me the page title"\n'
            "  python -m src_windows.main --model gpt-5.4 --max-steps 20\n"
            "  python -m src_windows.main --cua-only\n"
            "  python -m src_windows.main --llm-only\n"
        ),
    )
    parser.add_argument(
        "--task",
        type=str,
        default=DEFAULT_TASK,
        help="Task description for the agent",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5.4-mini",
        help="OpenAI model to use (default: gpt-5.4-mini)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=30,
        help="Max CUA loop iterations (default: 30)",
    )
    parser.add_argument(
        "--cua-only",
        action="store_true",
        help="Run only the Computer Use agent (skip normal LLM baseline)",
    )
    parser.add_argument(
        "--llm-only",
        action="store_true",
        help="Run only the normal LLM baseline (skip Computer Use)",
    )
    parser.add_argument(
        "--monitor",
        type=str,
        default="primary",
        help=(
            "Which screen the agent operates on. 'primary' (default) or '1' = "
            "primary monitor; '2', '3', ... = additional monitors in detected "
            "order; 'all' = full virtual desktop (less reliable on multi-monitor); "
            "'follow' = re-capture the foreground window's monitor after every "
            "step (recommended for tasks that span multiple apps/monitors)"
        ),
    )

    args = parser.parse_args()

    # Shared cost tracker for both runs
    tracker = CostTracker()

    console.print(
        Panel.fit(
            "[bold]OpenAI Computer Use POC (Windows)[/bold]\n"
            f"Model: [cyan]{args.model}[/cyan]\n"
            f"Task: [italic]{args.task[:80]}{'...' if len(args.task) > 80 else ''}[/italic]",
            title="🤖 CUA Cost Comparison",
            border_style="bright_blue",
        )
    )

    # ── Run normal LLM baseline ──────────────────────────────────────────
    if not args.cua_only:
        try:
            llm_result = run_normal_llm(
                task=args.task,
                model=args.model,
                tracker=tracker,
            )
            console.print(llm_result)
        except Exception as e:
            console.print(f"\n[red]✗ Normal LLM failed: {e}[/red]")
            if args.llm_only:
                sys.exit(1)

    # ── Run Computer Use agent ───────────────────────────────────────────
    if not args.llm_only:
        try:
            cua_result = run_computer_use(
                task=args.task,
                model=args.model,
                tracker=tracker,
                max_steps=args.max_steps,
                monitor=args.monitor,
            )
            console.print(cua_result)
        except Exception as e:
            console.print(f"\n[red]✗ Computer Use failed: {e}[/red]")
            import traceback

            traceback.print_exc()

    # ── Print cost comparison ────────────────────────────────────────────
    console.print()
    tracker.print_report()
    console.print()


if __name__ == "__main__":
    cli()

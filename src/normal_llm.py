"""
Normal LLM baseline — sends the same task as a plain text prompt (no tools).

This provides the cost baseline to compare against the Computer Use approach.
"""

from __future__ import annotations

import time
from typing import Any

from openai import OpenAI
from rich.console import Console

from .cost_tracker import CostTracker

console = Console()


def run_normal_llm(
    task: str,
    model: str = "gpt-5.5",
    tracker: CostTracker | None = None,
) -> dict[str, Any]:
    """
    Send the task as a plain text prompt to the same model — no computer tool.

    The prompt asks the model to perform the task "as if" it had a browser,
    describing what it would do and providing its best answer. This gives us
    a fair cost baseline: same model, same task, but text-only.

    Returns a dict with:
      - "final_output": the model's response text
      - "tracker": the CostTracker instance
    """
    if tracker is None:
        tracker = CostTracker()

    client = OpenAI()

    console.print("\n[bold blue]📝 Normal LLM Baseline[/bold blue]")
    console.print(f"  Task: [italic]{task}[/italic]")
    console.print(f"  Model: {model}\n")

    prompt = (
        "You are a helpful assistant. The user wants you to complete the following task. "
        "You do NOT have access to a browser or any tools. "
        "Do your best to answer based on your knowledge. "
        "If the task involves visiting a website, describe what you would expect to find "
        "and provide your best answer.\n\n"
        f"Task: {task}"
    )

    t0 = time.time()
    response = client.responses.create(
        model=model,
        input=prompt,
    )
    duration = time.time() - t0

    rec = tracker.record(response, "normal_llm", model, duration, "single_call")
    tracker.print_live_step(rec)

    # Extract text
    final_text = ""
    for item in response.output:
        if getattr(item, "type", None) == "message":
            content = getattr(item, "content", [])
            if isinstance(content, list):
                for part in content:
                    text = getattr(part, "text", None)
                    if text:
                        final_text += text
            elif isinstance(content, str):
                final_text += content

    if final_text:
        # Truncate for display
        display_text = final_text[:300]
        if len(final_text) > 300:
            display_text += "..."
        console.print(f"  [blue]Answer:[/blue] {display_text}")

    return {
        "final_output": final_text,
        "tracker": tracker,
    }

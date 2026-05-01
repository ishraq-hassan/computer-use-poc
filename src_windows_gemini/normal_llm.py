"""
Normal Gemini LLM baseline — sends the task as a plain text prompt (no tools).
Provides the cost baseline to compare against the Gemini Computer Use approach.
"""

from __future__ import annotations

import os
import time
from typing import Any

from google import genai
from google.genai import types
from rich.console import Console

from .cost_tracker import CostTracker

console = Console()


def run_normal_llm(
    task: str,
    model: str = "gemini-2.5-flash",
    tracker: CostTracker | None = None,
) -> dict[str, Any]:
    """Send the task as a plain text prompt to Gemini — no computer tool."""
    if tracker is None:
        tracker = CostTracker()

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key) if api_key else genai.Client()

    console.print("\n[bold blue]📝 Normal LLM Baseline (Gemini)[/bold blue]")
    console.print(f"  Task: [italic]{task}[/italic]")
    console.print(f"  Model: {model}\n")

    prompt = (
        "You are a helpful assistant. The user wants you to complete the following task. "
        "You do NOT have access to a browser or any tools. "
        "Do your best to answer based on your knowledge. "
        "If the task involves visiting a website or opening an app, describe what you "
        "would expect to find and provide your best answer.\n\n"
        f"Task: {task}"
    )

    t0 = time.time()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(),
    )
    duration = time.time() - t0

    rec = tracker.record(response, "normal_llm", model, duration, "single_call")
    tracker.print_live_step(rec)

    final_text = getattr(response, "text", "") or ""

    if final_text:
        display_text = final_text[:300]
        if len(final_text) > 300:
            display_text += "..."
        console.print(f"  [blue]Answer:[/blue] {display_text}")

    return {
        "final_output": final_text,
        "tracker": tracker,
    }

# Computer Use POC (Native macOS / Windows)

A proof-of-concept that demonstrates LLM-driven computer-use against your **actual desktop**, with a **cost tracking wrapper** for side-by-side comparison vs a plain text-only baseline.

Three variants live in this repo:

| Variant | Path | API |
|---|---|---|
| OpenAI macOS | `src_mac_gpt/` | [OpenAI Computer Use](https://developers.openai.com/api/docs/guides/tools-computer-use) |
| Gemini macOS | `src_mac_gemini/` | [Gemini Computer Use](https://ai.google.dev/gemini-api/docs/computer-use) (`gemini-2.5-computer-use-preview-10-2025`) |
| OpenAI Windows | `src_windows/` | [OpenAI Computer Use](https://developers.openai.com/api/docs/guides/tools-computer-use) |

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    main.py (CLI)                      │
│                                                       │
│  ┌─────────────────┐    ┌──────────────────────────┐ │
│  │  normal_llm.py   │    │   computer_use.py         │ │
│  │                   │    │                            │ │
│  │  Single text     │    │  Screenshot → Model →     │ │
│  │  prompt, no      │    │  Execute via pynput →     │ │
│  │  tools           │    │  (Native macOS)           │ │
│  └────────┬──────────┘    └────────────┬─────────────┘ │
│           │                            │               │
│           └──────────┬─────────────────┘               │
│                      ▼                                  │
│            ┌─────────────────┐                          │
│            │ cost_tracker.py  │                          │
│            └─────────────────┘                          │
└──────────────────────────────────────────────────────┘
```

## Setup (shared)

### 1. Install Python dependencies

```bash
cd computer-use-poc
uv sync
```

`pyobjc` only installs on macOS (gated by a `sys_platform == 'darwin'` marker), so the same `uv sync` works on both platforms.

### 2. Set your API key(s)

```bash
cp .env.example .env
# Edit .env and add OPENAI_API_KEY and/or GEMINI_API_KEY
```

## macOS

### Grant Accessibility Permissions

Your Terminal app (or IDE) needs **Accessibility** access to control the mouse and keyboard:
- Open **System Settings** > **Privacy & Security** > **Accessibility**.
- Add and enable your terminal/IDE.

### Usage — OpenAI variant

```bash
# Full comparison (normal LLM + Computer Use)
uv run python -m src_mac_gpt.main

# Custom macOS task
uv run python -m src_mac_gpt.main --task "Open the Reminders app and add a task to 'Buy Milk'"
```

### Usage — Gemini variant

Uses `gemini-2.5-computer-use-preview-10-2025` for the agent and `gemini-2.5-flash` (cost-effective) for the text baseline. The model emits coordinates on a normalized 0–999 grid, which we denormalize to logical pixels.

```bash
# Full comparison (normal LLM + Computer Use)
uv run python -m src_mac_gemini.main

# Even cheaper baseline
uv run python -m src_mac_gemini.main --llm-model gemini-2.5-flash-lite

# Custom task
uv run python -m src_mac_gemini.main --task "Open the Reminders app and add a task to 'Buy Milk'"
```

### How it works (macOS)

1.  **Actions**: The `computer` tool's actions are mapped to native macOS events using `pynput` and `Quartz`.
2.  **Visuals**: High-resolution screenshots are captured using the built-in `screencapture` utility, then resized to logical pixels via `sips` so model coords align.
3.  **Safety**: The agent runs directly on your machine. Exercise caution with the tasks you provide.
4.  **Cost Tracking**: Calculates the exact dollar cost of every visual turn vs. a standard text-only baseline.

## Windows

No special permissions required — pynput on Windows uses Win32 input APIs directly. Run the terminal as a regular user.

### Usage

```bash
# Full comparison (normal LLM + Computer Use). Defaults to operating on the primary monitor.
uv run python -m src_windows.main

# Custom Windows task
uv run python -m src_windows.main --task "Open Notepad and type 'Hello from computer use'"

# Operate on a specific monitor (1-indexed; 1 = primary)
uv run python -m src_windows.main --monitor 2

# Operate on the entire virtual desktop (less reliable on multi-monitor)
uv run python -m src_windows.main --monitor all
```

At startup the agent prints every monitor it detected with resolution and offset, so you can verify which `--monitor N` index maps to which physical screen on the host machine.

### How it works (Windows)

1.  **Actions**: The `computer` tool's actions are mapped to native Win32 input via `pyautogui` (not `pynput` — pynput's `SendInput` keystrokes silently fail to reach the foreground window on Windows 11 in practice; pyautogui's variant lands reliably). The Win key is reachable through `WIN`, `WINDOWS`, `META`, `SUPER`, or `CMD`. Move the cursor to a screen corner to abort the agent (`pyautogui.FAILSAFE = True`).
2.  **Per-monitor capture**: At startup the agent enumerates every monitor via `EnumDisplayMonitors` and selects one (default: primary) to operate on. Screenshots are bbox-cropped to that monitor's region in the virtual desktop, so the model sees a single normal-sized screen instead of a wide stitched image. Click coordinates from the model are screenshot-relative; we translate them by the chosen monitor's `(left, top)` offset before issuing the Win32 mouse call, so clicks land in the correct physical-pixel position. Pass `--monitor 2` (or `all`) to override.
3.  **DPI**: The process is marked **per-monitor DPI-aware** at module load (`SetProcessDpiAwareness(2)`). This forces `GetSystemMetrics`, `ImageGrab.grab`, and pynput's `SetCursorPos` to all operate in physical pixels — the model's screenshot, its click coordinates, and the mouse calls share one consistent space. Without this, `ImageGrab.grab` would silently flip the process to DPI-aware on its first call, causing the initial screen-rect query to be logical pixels while the screenshot is physical, and clicks on a HiDPI secondary monitor would land on the wrong position.
4.  **Cost Tracking**: Same wrapper as the macOS path — see `src_windows/cost_tracker.py`.

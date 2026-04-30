# OpenAI Computer Use POC (Native macOS / Windows)

A proof-of-concept that demonstrates OpenAI's [Computer Use API](https://developers.openai.com/api/docs/guides/tools-computer-use) targeting your **actual desktop** (macOS in `src_mac/`, Windows in `src_windows/`), with a **cost tracking wrapper**.

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

### 2. Set your API key

```bash
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

## macOS

### Grant Accessibility Permissions

Your Terminal app (or IDE) needs **Accessibility** access to control the mouse and keyboard:
- Open **System Settings** > **Privacy & Security** > **Accessibility**.
- Add and enable your terminal/IDE.

### Usage

```bash
# Full comparison (normal LLM + Computer Use)
uv run python -m src_mac.main

# Custom macOS task
uv run python -m src_mac.main --task "Open the Reminders app and add a task to 'Buy Milk'"
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
# Full comparison (normal LLM + Computer Use)
uv run python -m src_windows.main

# Custom Windows task
uv run python -m src_windows.main --task "Open Notepad and type 'Hello from computer use'"
```

### How it works (Windows)

1.  **Actions**: The `computer` tool's actions are mapped to native Win32 input via `pynput`. The Win key is reachable through `WIN`, `WINDOWS`, `META`, `SUPER`, or `CMD`.
2.  **Multi-monitor**: The agent captures the **entire virtual desktop** (every connected monitor) using `PIL.ImageGrab.grab(all_screens=True)`, so it can see and click on any display. Click coordinates from the model are screenshot-relative; we translate them by the virtual-screen offset (from `GetSystemMetrics(SM_XVIRTUALSCREEN/SM_YVIRTUALSCREEN)`) before calling pynput, which uses primary-monitor-origin coords.
3.  **DPI**: The process is marked **per-monitor DPI-aware** at module load (`SetProcessDpiAwareness(2)`). This forces `GetSystemMetrics`, `ImageGrab.grab`, and pynput's `SetCursorPos` to all operate in physical pixels — the model's screenshot, its click coordinates, and the mouse calls share one consistent space. Without this, `ImageGrab.grab` would silently flip the process to DPI-aware on its first call, causing the initial screen-rect query to be logical pixels while the screenshot is physical, and clicks on a HiDPI secondary monitor would land on the wrong position.
4.  **Cost Tracking**: Same wrapper as the macOS path — see `src_windows/cost_tracker.py`.

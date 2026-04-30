# OpenAI Computer Use POC (Native macOS)

A proof-of-concept that demonstrates OpenAI's [Computer Use API](https://developers.openai.com/api/docs/guides/tools-computer-use) targeting your **Actual macOS Desktop**, with a **cost tracking wrapper**.

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

## Setup

### 1. Install Python dependencies

```bash
cd computer-use-poc
uv sync
```

### 2. Grant Accessibility Permissions

Your Terminal app (or IDE) needs **Accessibility** access to control the mouse and keyboard:
- Open **System Settings** > **Privacy & Security** > **Accessibility**.
- Add and enable your terminal/IDE.

### 3. Set your API key

```bash
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

## Usage

### Full comparison (normal LLM + Computer Use)

```bash
uv run python -m src.main
```

### Custom macOS task

```bash
uv run python -m src.main --task "Open the Reminders app and add a task to 'Buy Milk'"
```

## How It Works (Native Mode)

1.  **Actions**: The `computer` tool's actions are mapped to native macOS events using `pynput` and `Quartz`.
2.  **Visuals**: High-resolution screenshots are captured using the built-in `screencapture` utility.
3.  **Safety**: The agent runs directly on your machine. Exercise caution with the tasks you provide.
4.  **Cost Tracking**: Calculates the exact dollar cost of every visual turn vs. a standard text-only baseline.

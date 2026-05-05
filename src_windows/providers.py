"""Gemini and OpenAI computer-use providers."""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from typing import Any

from .screen import Action, OPENAI_TARGET, downscale_screenshot, get_foreground_window_title


class Provider(ABC):
    name: str
    last_text: str = ""

    @abstractmethod
    def get_actions(self, screenshot: bytes, region_size: tuple[int, int]) -> list[Action] | None:
        """Send current screen, get back actions. None = agent is done."""


def _denorm(val: float, size: int) -> int:
    return int(float(val) / 1000.0 * size)


SYSTEM_PROMPT = """\
You are operating the user's Windows desktop.

RULES:
1. Do not group risky actions together. If an action could fail or change the
   screen, do it alone and wait for the next screenshot before continuing.
2. Limit actions per turn to 3. Fewer is better.
3. Prefer mouse clicks for visible targets. Keyboard only when nothing to click.
4. Verify each action visually. If something went wrong, recover first.
"""


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, task: str, model: str = "gemini-2.5-computer-use-preview-10-2025"):
        from google import genai
        from google.genai import types
        from google.genai.types import Content, Part

        self._types = types
        self._Content = Content
        self._Part = Part

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self._model = model
        self._task = task
        self._contents: list = []
        self._pending_fcs: list = []
        self._step = 0
        self._config = types.GenerateContentConfig(
            tools=[types.Tool(computer_use=types.ComputerUse(
                environment=types.Environment.ENVIRONMENT_BROWSER,
            ))],
            system_instruction=SYSTEM_PROMPT,
        )

    def get_actions(self, screenshot: bytes, region_size: tuple[int, int]) -> list[Action] | None:
        types = self._types
        Content, Part = self._Content, self._Part
        rw, rh = region_size

        if self._step == 0:
            self._contents.append(Content(role="user", parts=[
                Part(text=self._task),
                Part.from_bytes(data=screenshot, mime_type="image/png"),
            ]))
        else:
            frs = []
            for fc in self._pending_fcs:
                frs.append(types.FunctionResponse(
                    name=fc.name, response={"url": ""},
                    parts=[types.FunctionResponsePart(
                        inline_data=types.FunctionResponseBlob(
                            mime_type="image/png", data=screenshot,
                        ),
                    )],
                ))
            if frs:
                self._contents.append(Content(
                    role="user",
                    parts=[Part(function_response=fr) for fr in frs],
                ))

        self._step += 1
        response = self._client.models.generate_content(
            model=self._model, contents=self._contents, config=self._config,
        )

        if not response.candidates:
            return None
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            return None

        self._contents.append(candidate.content)
        fcs = [p.function_call for p in candidate.content.parts if p.function_call]
        self._pending_fcs = fcs
        self.last_text = "".join(
            p.text for p in candidate.content.parts if getattr(p, "text", None)
        )

        return self._convert(fcs, rw, rh) if fcs else None

    def _convert(self, fcs, rw: int, rh: int) -> list[Action]:
        actions: list[Action] = []
        for fc in fcs:
            name = fc.name
            a = dict(fc.args or {})

            if name == "click_at":
                actions.append(Action(type="click",
                    x=_denorm(a["x"], rw), y=_denorm(a["y"], rh)))

            elif name == "type_text_at":
                actions.append(Action(type="type",
                    x=_denorm(a["x"], rw), y=_denorm(a["y"], rh),
                    text=a.get("text", "")))

            elif name == "hover_at":
                actions.append(Action(type="move",
                    x=_denorm(a["x"], rw), y=_denorm(a["y"], rh)))

            elif name == "key_combination":
                tokens = [t for t in (a.get("keys", "")).replace(" ", "").split("+") if t]
                if tokens:
                    actions.append(Action(type="keypress", keys=tokens))

            elif name in ("scroll_document", "scroll_at"):
                d = (a.get("direction") or "down").lower()
                mag = 10
                if name == "scroll_at":
                    mag = max(1, int(a.get("magnitude", 800)) // 100)
                dy = mag if d == "up" else -mag if d == "down" else 0
                dx = mag if d == "right" else -mag if d == "left" else 0
                x = _denorm(a["x"], rw) if "x" in a else None
                y = _denorm(a["y"], rh) if "y" in a else None
                actions.append(Action(type="scroll", x=x, y=y, scroll_y=dy, scroll_x=dx))

            elif name == "drag_and_drop":
                actions.append(Action(type="drag", drag_path=[
                    (_denorm(a["x"], rw), _denorm(a["y"], rh)),
                    (_denorm(a["destination_x"], rw), _denorm(a["destination_y"], rh)),
                ]))

            elif name == "wait_5_seconds":
                actions.append(Action(type="wait"))

            elif name == "navigate":
                actions.append(Action(type="navigate", text=a.get("url", "")))

        return actions




class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, task: str, model: str = "gpt-5.4-mini"):
        from openai import OpenAI
        self._client = OpenAI()
        self._model = model
        self._task = task
        self._step = 0
        self._prev_response: Any = None
        self._prev_call_id: str | None = None
        self._scale_x: float = 1.0
        self._scale_y: float = 1.0

    def _screen_context(self) -> str:
        w, h = OPENAI_TARGET
        fg = get_foreground_window_title()
        ctx = f"Screenshot is {w}x{h}. Valid click range: (0, 0) to ({w - 1}, {h - 1})."
        if fg:
            ctx += f"\nForeground window (receives keyboard input): {fg!r}"
        return ctx

    def _to_screen(self, x: int, y: int) -> tuple[int, int]:
        return int(x * self._scale_x), int(y * self._scale_y)

    def get_actions(self, screenshot: bytes, region_size: tuple[int, int]) -> list[Action] | None:
        scaled, self._scale_x, self._scale_y = downscale_screenshot(screenshot)
        b64 = base64.b64encode(scaled).decode()

        if self._step == 0:
            response = self._client.responses.create(
                model=self._model,
                tools=[{"type": "computer"}],
                input=self._task,
                instructions=SYSTEM_PROMPT,
                truncation="auto",
            )
        else:
            ctx = self._screen_context()
            response = self._client.responses.create(
                model=self._model,
                tools=[{"type": "computer"}],
                previous_response_id=self._prev_response.id,
                instructions=SYSTEM_PROMPT,
                truncation="auto",
                input=[
                    {
                        "type": "computer_call_output",
                        "call_id": self._prev_call_id,
                        "output": {
                            "type": "computer_screenshot",
                            "image_url": f"data:image/png;base64,{b64}",
                            "detail": "original",
                        },
                    },
                    {"role": "user", "content": ctx},
                ],
            )

        self._step += 1
        self._prev_response = response

        self.last_text = ""
        for item in response.output:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                for part in getattr(item, "content", []):
                    if getattr(part, "text", None):
                        self.last_text += part.text
            elif item_type == "reasoning":
                for part in getattr(item, "content", []):
                    if getattr(part, "text", None):
                        self.last_text += part.text

        computer_call = next(
            (item for item in response.output
             if getattr(item, "type", None) == "computer_call"),
            None,
        )
        if computer_call is None:
            return None

        self._prev_call_id = computer_call.call_id
        return self._convert(computer_call.actions)

    def _convert(self, raw) -> list[Action]:
        actions: list[Action] = []
        for r in raw:
            t = getattr(r, "type", None)
            if t == "screenshot":
                continue
            mods = list(r.keys) if getattr(r, "keys", None) else None

            if t == "click":
                sx, sy = self._to_screen(r.x, r.y)
                actions.append(Action(type="click", x=sx, y=sy,
                    button=getattr(r, "button", "left") or "left", modifiers=mods))

            elif t == "double_click":
                sx, sy = self._to_screen(r.x, r.y)
                actions.append(Action(type="double_click", x=sx, y=sy, modifiers=mods))

            elif t == "type":
                actions.append(Action(type="type", text=r.text))

            elif t == "keypress":
                actions.append(Action(type="keypress", keys=list(r.keys)))

            elif t == "scroll":
                sx, sy = self._to_screen(r.x, r.y)
                actions.append(Action(type="scroll", x=sx, y=sy,
                    scroll_x=getattr(r, "scrollX", 0) or 0,
                    scroll_y=getattr(r, "scrollY", 0) or 0, modifiers=mods))

            elif t == "move":
                sx, sy = self._to_screen(r.x, r.y)
                actions.append(Action(type="move", x=sx, y=sy, modifiers=mods))

            elif t == "drag":
                path = []
                for p in r.path:
                    if isinstance(p, dict):
                        path.append(self._to_screen(p["x"], p["y"]))
                    elif isinstance(p, (list, tuple)):
                        path.append(self._to_screen(p[0], p[1]))
                    else:
                        path.append(self._to_screen(p.x, p.y))
                actions.append(Action(type="drag", drag_path=path, modifiers=mods))

            elif t == "wait":
                actions.append(Action(type="wait"))

        return actions

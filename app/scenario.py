"""Scenario loading. A scenario is the 'agent context': persona, goals, rules, knowledge."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"


@dataclass
class Scenario:
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw["id"]

    @property
    def org(self) -> dict[str, Any]:
        return self.raw["organization"]

    @property
    def agent(self) -> dict[str, Any]:
        return self.raw["agent"]

    @property
    def opening_line(self) -> str:
        return self.raw["opening_line"]

    @property
    def closing_line(self) -> str:
        return self.raw.get("closing_line", "Thanks for calling. Goodbye!")

    @property
    def required_fields(self) -> dict[str, str]:
        return self.raw.get("required_fields", {})

    @property
    def optional_fields(self) -> dict[str, str]:
        return self.raw.get("optional_fields", {})

    @property
    def tool_fillers(self) -> dict[str, list[str]]:
        return self.raw.get("tool_fillers", {})

    @property
    def silence_prompts(self) -> list[str]:
        return self.raw.get("silence_prompts", ["Are you still there?"])

    @property
    def max_call_seconds(self) -> int:
        return int(self.raw.get("max_call_seconds", 480))

    @property
    def timezone(self) -> str:
        return self.org.get("timezone", "America/Chicago")


def load_scenario(name_or_path: str) -> Scenario:
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.json"
    with path.open() as f:
        return Scenario(json.load(f))

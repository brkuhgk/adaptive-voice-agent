"""Agent profiles. A profile is everything that makes one kind of call different from another:
who the agent is, what it knows, which tools it has, how state is tracked, and which voice it uses.

    ClinicProfile    inbound calls to the clinic front desk (scenarios/clinic.json)
    CampaignProfile  outbound calls to a contact list (campaigns/<name>/), see app/campaigns.py

The Agent and CallSession only talk to this interface, so new call types need no changes there.
"""
from __future__ import annotations

from typing import Any

from .prompt import build_system_prompt, build_tracker_messages
from .scenario import Scenario
from .scheduling import ClinicSchedule
from .state import INTENTS, ConversationState
from .tools import TOOL_SCHEMAS, ToolExecutor


class AgentProfile:
    kind = "base"
    agent_name = "Agent"
    org_name = ""
    voice_id: str | None = None          # None = use ELEVENLABS_VOICE_ID
    opening_line = "Hello?"
    closing_line = "Thanks for your time. Goodbye!"
    voicemail_text: str | None = None
    opt_out_digit: str | None = None
    max_call_seconds = 480
    tool_fillers: dict[str, list[str]] = {}
    tools: list[dict[str, Any]] = []
    keyterms: list[str] = []

    def new_state(self, call_id: str, caller: str | None) -> ConversationState:
        raise NotImplementedError

    def system_prompt(self, state: ConversationState) -> str:
        raise NotImplementedError

    def execute_tool(self, state: ConversationState, name: str, raw_args: str) -> dict[str, Any]:
        raise NotImplementedError

    def tracker_messages(self, state: ConversationState) -> list[dict[str, str]]:
        raise NotImplementedError

    def opt_out(self, state: ConversationState, how: str) -> None:
        """Caller asked not to be called again (spoken or keypad)."""

    def on_call_end(self, state: ConversationState) -> None:
        """Persist results, if this profile keeps any."""


class ClinicProfile(AgentProfile):
    kind = "clinic"

    def __init__(self, scenario: Scenario, schedule: ClinicSchedule):
        self.scenario = scenario
        self.schedule = schedule
        self.agent_name = scenario.agent["name"]
        self.org_name = scenario.org["name"]
        self.opening_line = scenario.opening_line
        self.closing_line = scenario.closing_line
        self.max_call_seconds = scenario.max_call_seconds
        self.tool_fillers = scenario.tool_fillers
        self.voice_id = scenario.raw.get("voice_id") or None
        self.tools = TOOL_SCHEMAS
        names = [self.org_name, self.agent_name]
        for p in scenario.raw.get("knowledge", {}).get("providers", []):
            names.append(p["name"].replace("Dr. ", "").replace(", NP", ""))
        self.keyterms = names

    def new_state(self, call_id: str, caller: str | None) -> ConversationState:
        return ConversationState(
            call_id=call_id, caller_number=caller, required_fields=list(self.scenario.required_fields),
            optional_fields=list(self.scenario.optional_fields), allowed_intents=INTENTS,
            labels={"profile": "clinic"},
        )

    def system_prompt(self, state: ConversationState) -> str:
        return build_system_prompt(self.scenario, state, self.schedule)

    def execute_tool(self, state: ConversationState, name: str, raw_args: str) -> dict[str, Any]:
        return ToolExecutor(self.schedule, state).execute(name, raw_args)

    def tracker_messages(self, state: ConversationState) -> list[dict[str, str]]:
        return build_tracker_messages(state, self.schedule.today().isoformat())

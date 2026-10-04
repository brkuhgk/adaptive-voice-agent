"""Text-only conversation with the same brain. Used by the dashboard's 'type as caller' box,
the CLI simulator, and `scripts/campaign.py chat`: test the agent without a phone."""
from __future__ import annotations

import uuid

from .agent import Agent
from .config import Settings
from .events import EventBus
from .llm import LLMClient
from .profiles import AgentProfile


class TextSession:
    def __init__(self, profile: AgentProfile, llm: LLMClient, bus: EventBus, settings: Settings,
                 session_id: str | None = None):
        self.id = session_id or f"text-{uuid.uuid4().hex[:6]}"
        self.profile = profile
        self.state = profile.new_state(self.id, "text-sim")
        self.state.labels["text_mode"] = True
        self.agent = Agent(profile, self.state, llm, bus, settings)
        who = self.state.labels.get("contact_name") or "text simulator"
        bus.publish(self.id, "call_started", profile=profile.kind, caller=f"{who} (text)",
                    agent=profile.agent_name, org=profile.org_name, labels=self.state.labels)
        self.agent.add_assistant_text(profile.opening_line)
        self.agent.publish_state("start")
        self.opening_line = profile.opening_line

    async def say(self, text: str) -> str:
        parts = [p async for p in self.agent.respond(text)]
        # Let the parallel state tracker land so the reply and state stay in sync for the UI.
        task = self.agent._tracker_task
        if task is not None and not task.done():
            try:
                await task
            except Exception:
                pass
        if self.ended:
            self.profile.on_call_end(self.state)
        return "".join(parts).strip()

    @property
    def ended(self) -> bool:
        return self.state.end_requested

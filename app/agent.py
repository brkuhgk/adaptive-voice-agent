"""The conversation brain.

Each caller turn:
  1. the utterance is appended to the history and the transcript;
  2. a background *state tracker* call extracts structured state (intent, details, sentiment, plan);
  3. the *responder* is called with   system prompt = agent context + live state snapshot
                                       messages      = trimmed conversation history
     and streams text back token by token, calling tools when it needs grounded data or actions.

Nothing here is a fixed script: what gets said next is decided by the model from the context + state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, AsyncIterator

from .config import Settings
from .events import EventBus
from .llm import LLMClient
from .profiles import AgentProfile
from .state import ConversationState

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
FALLBACK_REPLY = "Sorry, I'm having a little trouble on my end. Could you say that one more time?"


class Agent:
    def __init__(self, profile: AgentProfile, state: ConversationState, llm: LLMClient, bus: EventBus,
                 settings: Settings):
        self.profile = profile
        self.state = state
        self.llm = llm
        self.bus = bus
        self.settings = settings
        self.messages: list[dict[str, Any]] = []
        self._tracker_task: asyncio.Task | None = None
        self._last_reply_msg: int | None = None
        self._last_reply_transcript: int | None = None
        self._resp_used_tools = False
        self._last_filler: str | None = None

    # ----------------------------------------------------------------- helpers
    def _publish(self, type_: str, **data: Any) -> None:
        self.bus.publish(self.state.call_id, type_, **data)

    def publish_state(self, source: str, changed: list[str] | None = None) -> None:
        self._publish("state", state=self.state.to_dict(), source=source, changed=changed or [])

    def _history(self) -> list[dict[str, Any]]:
        limit = self.settings.max_history_messages
        msgs = self.messages if len(self.messages) <= limit else self.messages[-limit:]
        # Never start on a tool result or an assistant tool-call fragment.
        while msgs and msgs[0]["role"] != "user":
            msgs = msgs[1:]
        return msgs

    def _filler(self, tool_name: str) -> str | None:
        options = [f for f in self.profile.tool_fillers.get(tool_name, []) if f != self._last_filler]
        if not options:
            return None
        self._last_filler = random.choice(options)
        return self._last_filler

    # ------------------------------------------------------------- public API
    def add_assistant_text(self, text: str) -> None:
        """Record something the agent says verbatim (opening line, canned closing)."""
        self.messages.append({"role": "assistant", "content": text})
        self._last_reply_msg = len(self.messages) - 1
        self.state.add_transcript("agent", text)
        self._last_reply_transcript = len(self.state.transcript) - 1
        self._publish("agent_turn", text=text, interrupted=False)

    def respond(self, user_text: str, *, is_event: bool = False) -> AsyncIterator[str]:
        """Record the caller turn now (eagerly), and return a stream of the agent's spoken reply.

        Bookkeeping happens before the stream is consumed so that cancelling the reply
        before it ever starts still leaves a consistent history."""
        self.state.turns += 1
        self.messages.append({"role": "user", "content": user_text})
        self.state.add_transcript("event" if is_event else "caller", user_text)
        self._publish("system_event" if is_event else "user_turn", text=user_text)
        if not is_event:
            self.schedule_tracking()
        self._resp_used_tools = False
        self._last_reply_msg = None
        self._last_reply_transcript = None
        return self._stream_reply()

    async def _stream_reply(self) -> AsyncIterator[str]:
        spoken: list[str] = []
        round_start = 0  # index in `spoken` where the current LLM round began

        def said(since: int = 0) -> str:
            return "".join(spoken[since:]).strip()

        try:
            failed = False
            try:
                for _round in range(MAX_TOOL_ROUNDS):
                    round_start = len(spoken)
                    calls: dict[int, dict[str, str]] = {}
                    system = self.profile.system_prompt(self.state)
                    messages = [{"role": "system", "content": system}, *self._history()]
                    async for d in self.llm.stream_chat(messages, self.profile.tools):
                        if d.text:
                            spoken.append(d.text)
                            self._publish("agent_delta", text=d.text)
                            yield d.text
                        if d.tool_index is not None:
                            c = calls.setdefault(d.tool_index, {"id": "", "name": "", "args": ""})
                            c["id"] = d.tool_id or c["id"]
                            c["name"] = d.tool_name or c["name"]
                            c["args"] += d.tool_args or ""
                            if c["name"] and not said():
                                filler = self._filler(c["name"])
                                if filler:  # mask tool latency with a natural "one sec"
                                    spoken.append(filler + " ")
                                    self._publish("agent_delta", text=filler + " ")
                                    yield filler + " "
                    if not calls:
                        break
                    # Text spoken before the tool call (e.g. the filler) goes on the tool-call
                    # message so the model knows it already said "one sec" and doesn't repeat it.
                    self._run_tools(calls, said(round_start) or None)
                else:
                    log.warning("tool loop limit reached")
            except Exception:
                log.exception("LLM turn failed")
                failed = True

            if failed and not said():
                spoken.append(FALLBACK_REPLY)
                yield FALLBACK_REPLY
            if self.state.end_requested and not said():
                spoken.append(self.profile.closing_line)
                yield self.profile.closing_line
        except (asyncio.CancelledError, GeneratorExit):
            # Interrupted mid-reply: keep the history valid; the call session will
            # rewrite this with what the caller actually heard.
            self._append_reply(said(round_start) or "[about to answer]", said(), final=False)
            raise
        self._append_reply(said(round_start) or ("" if said() else "(no reply)"), said() or "(no reply)", final=True)

    def _append_reply(self, history_text: str, full_text: str, final: bool) -> None:
        if history_text:
            self.messages.append({"role": "assistant", "content": history_text})
            self._last_reply_msg = len(self.messages) - 1
        else:
            # Everything was said before the last tool call; that message already holds it.
            self._last_reply_msg = max(i for i, m in enumerate(self.messages) if m["role"] == "assistant")
        if final:
            self.state.add_transcript("agent", full_text)
            self._last_reply_transcript = len(self.state.transcript) - 1
            self._publish("agent_turn", text=full_text, interrupted=False)
        else:
            self._last_reply_transcript = None

    def _run_tools(self, calls: dict[int, dict[str, str]], spoken_before: str | None) -> None:
        self._resp_used_tools = True
        ordered = [calls[i] for i in sorted(calls)]
        self.messages.append({
            "role": "assistant", "content": spoken_before,
            "tool_calls": [{"id": c["id"] or f"call_{i}", "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                           for i, c in enumerate(ordered)],
        })
        for i, c in enumerate(ordered):
            result = self.profile.execute_tool(self.state, c["name"], c["args"])
            self.messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}",
                                  "content": json.dumps(result)})
            try:
                args = json.loads(c["args"] or "{}")
            except json.JSONDecodeError:
                args = c["args"]
            self._publish("tool_call", name=c["name"], args=args, result=result)
        self.publish_state("tool")

    def record_interruption(self, heard: str) -> None:
        """The caller barged in. Rewrite the last reply so the model knows what was actually heard."""
        self.state.interruptions += 1
        heard = heard.strip()
        note = f"{heard} [caller interrupted here]" if heard else "[caller interrupted before hearing this reply]"
        if self._last_reply_msg is not None and self._last_reply_msg < len(self.messages):
            self.messages[self._last_reply_msg]["content"] = note
        if self._last_reply_transcript is not None:
            entry = self.state.transcript[self._last_reply_transcript]
            entry.update({"text": heard or "(cut off)", "interrupted": True})
        else:
            self.state.add_transcript("agent", heard or "(cut off)", interrupted=True)
        self._publish("agent_turn", text=heard, interrupted=True)

    def retract_last_turn(self) -> str | None:
        """Undo the latest caller turn (used when the caller keeps talking before we said anything).

        Returns the retracted text so it can be merged with what the caller said next."""
        if self._resp_used_tools:
            return None
        for idx in range(len(self.messages) - 1, -1, -1):
            if self.messages[idx]["role"] == "user":
                text = self.messages[idx]["content"]
                del self.messages[idx:]
                for t_idx in range(len(self.state.transcript) - 1, -1, -1):
                    if self.state.transcript[t_idx]["role"] == "caller":
                        del self.state.transcript[t_idx:]
                        break
                self.state.turns -= 1
                self._last_reply_msg = None
                self._last_reply_transcript = None
                self._publish("retract", text=text)
                return text
        return None

    # ----------------------------------------------------------- state tracker
    def schedule_tracking(self) -> None:
        if not self.settings.state_tracker_enabled:
            return
        if self._tracker_task and not self._tracker_task.done():
            self._tracker_task.cancel()
        self._tracker_task = asyncio.create_task(self._track())

    async def _track(self) -> None:
        try:
            update = await self.llm.complete_json(self.profile.tracker_messages(self.state))
            changed = self.state.merge_tracker_update(update or {})
            self.publish_state("tracker", changed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("state tracker failed")

    async def close(self) -> None:
        if self._tracker_task and not self._tracker_task.done():
            self._tracker_task.cancel()
            try:
                await self._tracker_task
            except (asyncio.CancelledError, Exception):
                pass

"""Conversation state: what the agent knows, what's missing, and where the call is heading."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

INTENTS = {"new_appointment", "reschedule", "cancel", "question", "other", "unknown"}
URGENCY_LEVELS = ("none", "same_day", "emergency")


@dataclass
class ConversationState:
    call_id: str
    required_fields: list[str]
    optional_fields: list[str]
    caller_number: str | None = None
    started_at: float = field(default_factory=time.time)

    intent: str = "unknown"
    fields: dict[str, str | None] = field(default_factory=dict)
    urgency: str = "none"
    urgency_reason: str | None = None
    caller_sentiment: str = "neutral"
    next_best_action: str | None = None

    offered_slots: list[dict[str, Any]] = field(default_factory=list)
    appointment: dict[str, Any] | None = None
    cancelled: list[dict[str, Any]] = field(default_factory=list)
    end_requested: bool = False
    end_reason: str | None = None

    # Outbound campaign calls
    outcome: str | None = None
    summary: str | None = None
    callback_time: str | None = None
    opted_out: bool = False
    labels: dict[str, Any] = field(default_factory=dict)  # e.g. campaign, contact_id, contact_name

    turns: int = 0
    interruptions: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)

    # Profile hooks: which intents the tracker may set, and how to derive the phase.
    allowed_intents: set[str] | None = field(default=None, repr=False)
    phase_fn: Callable[["ConversationState"], str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in [*self.required_fields, *self.optional_fields]:
            self.fields.setdefault(name, None)

    # ----------------------------------------------------------- derived state
    @property
    def missing_required(self) -> list[str]:
        return [f for f in self.required_fields if not self.fields.get(f)]

    @property
    def phase(self) -> str:
        if self.phase_fn is not None:
            return self.phase_fn(self)
        return self._clinic_phase()

    def _clinic_phase(self) -> str:
        if self.end_requested:
            return "wrap_up"
        if self.urgency == "emergency":
            return "emergency"
        if self.appointment:
            return "confirmed"
        if self.intent == "unknown":
            return "identify_need"
        if self.intent in {"question", "other"}:
            return "answering"
        if self.intent == "cancel":
            return "cancelling"
        if self.missing_required:
            return "collecting_details"
        if self.offered_slots:
            return "choosing_slot"
        return "scheduling"

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    # -------------------------------------------------------------- mutations
    def add_transcript(self, role: str, text: str, **extra: Any) -> None:
        self.transcript.append({"role": role, "text": text, "t": round(self.elapsed_s, 2), **extra})

    def merge_tracker_update(self, update: dict[str, Any]) -> list[str]:
        """Merge a state-tracker JSON result. Only non-empty values overwrite. Returns changed keys."""
        changed: list[str] = []
        intent = update.get("intent")
        allowed = self.allowed_intents if self.allowed_intents is not None else INTENTS
        if isinstance(intent, str) and intent in allowed and intent != "unknown" and intent != self.intent:
            self.intent = intent
            changed.append("intent")
        for key in self.fields:
            val = update.get(key)
            if isinstance(val, (str, int, float)) and str(val).strip() and str(val).strip().lower() not in {"null", "none", "unknown"}:
                val = str(val).strip()
                if self.fields.get(key) != val:
                    self.fields[key] = val
                    changed.append(key)
        sentiment = update.get("caller_sentiment")
        if isinstance(sentiment, str) and sentiment.strip() and sentiment != self.caller_sentiment:
            self.caller_sentiment = sentiment.strip()
            changed.append("caller_sentiment")
        nba = update.get("next_best_action")
        if isinstance(nba, str) and nba.strip():
            self.next_best_action = nba.strip()
            changed.append("next_best_action")
        return changed

    def set_urgency(self, level: str, reason: str) -> None:
        # Urgency only escalates during a call; it never silently downgrades.
        if level in URGENCY_LEVELS and URGENCY_LEVELS.index(level) >= URGENCY_LEVELS.index(self.urgency):
            self.urgency = level
            self.urgency_reason = reason

    # ---------------------------------------------------------------- views
    def prompt_snapshot(self) -> str:
        """Compact, human-readable state block injected into the system prompt every turn."""
        known = {k: v for k, v in self.fields.items() if v}
        lines = [
            f"phase: {self.phase}",
            f"caller intent: {self.intent}",
            f"known details: {known if known else 'none yet'}",
            f"still needed (required): {', '.join(self.missing_required) or 'nothing, all collected'}",
            f"caller sentiment: {self.caller_sentiment}",
            f"urgency: {self.urgency}" + (f" ({self.urgency_reason})" if self.urgency_reason else ""),
        ]
        if self.offered_slots:
            lines.append("slots you offered: " + "; ".join(f"{s['slot_id']} = {s['description']}" for s in self.offered_slots))
        if self.appointment:
            lines.append(f"booked appointment: {self.appointment}")
        if self.cancelled:
            lines.append(f"cancelled this call: {[c.get('appointment_id') for c in self.cancelled]}")
        if self.outcome:
            lines.append(f"recorded outcome: {self.outcome}")
        if self.next_best_action:
            lines.append(f"planner suggestion (use judgement): {self.next_best_action}")
        lines.append(f"call duration so far: {int(self.elapsed_s)} seconds")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "caller_number": self.caller_number,
            "phase": self.phase,
            "intent": self.intent,
            "fields": self.fields,
            "required_fields": self.required_fields,
            "missing_required": self.missing_required,
            "urgency": self.urgency,
            "urgency_reason": self.urgency_reason,
            "caller_sentiment": self.caller_sentiment,
            "next_best_action": self.next_best_action,
            "offered_slots": self.offered_slots,
            "appointment": self.appointment,
            "end_requested": self.end_requested,
            "outcome": self.outcome,
            "summary": self.summary,
            "callback_time": self.callback_time,
            "opted_out": self.opted_out,
            "labels": self.labels,
            "turns": self.turns,
            "interruptions": self.interruptions,
            "elapsed_s": round(self.elapsed_s, 1),
        }

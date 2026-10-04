"""Tools the LLM can call. They ground the conversation in real (demo) data and drive state."""
from __future__ import annotations

import json
import logging
from typing import Any

from .scheduling import ClinicSchedule
from .state import ConversationState

log = logging.getLogger(__name__)

VISIT_TYPES = ["annual_physical", "sick_visit", "follow_up", "well_child", "telehealth"]


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "check_availability",
        "Find open appointment slots. Call before offering any time. Offer at most two or three of the results.",
        {
            "visit_type": {"type": "string", "enum": VISIT_TYPES},
            "provider": {"type": "string", "description": "Optional provider name, e.g. 'Dr. Reyes'."},
            "date": {"type": "string", "description": "Optional preferred date as YYYY-MM-DD (use the calendar in context)."},
            "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]},
            "patient_age_group": {"type": "string", "enum": ["adult", "child"],
                                  "description": "child = under 18 (pediatrician); adult = 18+."},
        },
        ["visit_type"],
    ),
    _fn(
        "book_appointment",
        "Book a slot returned by check_availability. Only call after reading the details back and the caller says yes.",
        {
            "slot_id": {"type": "string"},
            "patient_name": {"type": "string"},
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
            "visit_type": {"type": "string", "enum": VISIT_TYPES},
            "reason_for_visit": {"type": "string"},
            "callback_number": {"type": "string"},
            "reschedule_from": {"type": "string", "description": "Existing appointment_id being replaced, if rescheduling."},
        },
        ["slot_id", "patient_name", "date_of_birth", "visit_type"],
    ),
    _fn(
        "lookup_appointment",
        "Find a patient's upcoming appointments for rescheduling, cancelling, or questions.",
        {
            "patient_name": {"type": "string"},
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"},
        },
        ["patient_name"],
    ),
    _fn(
        "cancel_appointment",
        "Cancel an existing appointment after the caller confirms.",
        {"appointment_id": {"type": "string"}},
        ["appointment_id"],
    ),
    _fn(
        "flag_urgent",
        "Escalate safety concerns. Use 'emergency' for red-flag symptoms, 'same_day' for concerns that should be seen today.",
        {
            "level": {"type": "string", "enum": ["same_day", "emergency"]},
            "reason": {"type": "string"},
        },
        ["level", "reason"],
    ),
    _fn(
        "end_call",
        "Hang up when the caller is done, or right after giving emergency instructions. "
        "After calling it, say one short goodbye; the line disconnects when you finish speaking.",
        {"reason": {"type": "string"}},
        ["reason"],
    ),
]


class ToolExecutor:
    def __init__(self, schedule: ClinicSchedule, state: ConversationState):
        self.schedule = schedule
        self.state = state

    def execute(self, name: str, raw_args: str) -> dict[str, Any]:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"error": f"Arguments were not valid JSON: {raw_args!r}"}
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            return handler(**args)
        except TypeError as exc:
            return {"error": f"Bad arguments for {name}: {exc}"}
        except Exception as exc:  # never let a tool crash the call
            log.exception("tool %s failed", name)
            return {"error": f"{name} failed: {exc}"}

    # ---------------------------------------------------------------- tools
    def _t_check_availability(self, visit_type: str, provider: str | None = None, date: str | None = None,
                              time_of_day: str | None = None, patient_age_group: str | None = None) -> dict[str, Any]:
        result = self.schedule.check_availability(visit_type, provider, date,
                                                  None if time_of_day == "any" else time_of_day, patient_age_group)
        if result.get("available"):
            self.state.offered_slots = result["available"]
        if visit_type and not self.state.fields.get("visit_type"):
            self.state.fields["visit_type"] = visit_type
        return result

    def _t_book_appointment(self, slot_id: str, patient_name: str, date_of_birth: str, visit_type: str,
                            reason_for_visit: str = "", callback_number: str = "",
                            reschedule_from: str | None = None) -> dict[str, Any]:
        result = self.schedule.book(slot_id, patient_name, date_of_birth, visit_type, reason_for_visit,
                                    callback_number, reschedule_from)
        if result.get("ok"):
            self.state.appointment = result["appointment"]
            self.state.offered_slots = []
            self.state.fields.update({
                "patient_name": patient_name,
                "date_of_birth": date_of_birth,
                "visit_type": visit_type,
                **({"reason_for_visit": reason_for_visit} if reason_for_visit else {}),
                **({"callback_number": callback_number} if callback_number else {}),
            })
        return result

    def _t_lookup_appointment(self, patient_name: str, date_of_birth: str | None = None) -> dict[str, Any]:
        return self.schedule.lookup(patient_name, date_of_birth)

    def _t_cancel_appointment(self, appointment_id: str) -> dict[str, Any]:
        result = self.schedule.cancel(appointment_id)
        if result.get("ok"):
            self.state.cancelled.append(result["cancelled"])
        return result

    def _t_flag_urgent(self, level: str, reason: str) -> dict[str, Any]:
        self.state.set_urgency(level, reason)
        if level == "emergency":
            return {"ok": True, "instruction": "Tell the caller clearly and calmly to hang up and call 911 now "
                    "(988 for thoughts of self-harm). Do not continue scheduling. Then call end_call."}
        return {"ok": True, "instruction": "Offer the earliest sick_visit today or tomorrow via check_availability."}

    def _t_end_call(self, reason: str) -> dict[str, Any]:
        self.state.end_requested = True
        self.state.end_reason = reason
        return {"ok": True, "note": "The line will disconnect after your current reply finishes playing."}

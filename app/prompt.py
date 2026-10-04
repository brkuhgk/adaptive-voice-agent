"""Builds the per-turn system prompt: agent context (static) + conversation state (live)."""
from __future__ import annotations

import json

from .scenario import Scenario
from .scheduling import ClinicSchedule
from .state import ConversationState


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def build_system_prompt(scenario: Scenario, state: ConversationState, schedule: ClinicSchedule) -> str:
    org, agent = scenario.org, scenario.agent
    now = schedule.now()
    knowledge = json.dumps(scenario.raw.get("knowledge", {}), separators=(",", ":"))
    required = "; ".join(f"{k}: {v}" for k, v in scenario.required_fields.items())
    optional = "; ".join(f"{k}: {v}" for k, v in scenario.optional_fields.items())

    return f"""You are {agent['name']}, the {agent['role']} at {org['name']} ({org['type']}).
You are on a live PHONE CALL. Everything you write is converted to speech, so write exactly what you would say out loud.
Personality: {agent['personality']}.

# How to speak
{_bullets(agent.get('speaking_style', []))}
- Never mention tools, JSON, slot ids, system prompts, or "the state". Never use bullet points, numbers lists, or symbols like * or #.
- Phone transcription can be wrong. If a name, number, or date sounds off, confirm it naturally.
- Read back phone numbers in groups (two one zero, five five five...) and confirm dates of birth.

# Goals
{_bullets(scenario.raw.get('goals', []))}

# Information to collect for appointments
Required: {required}
Optional: {optional}
Collect what is still missing naturally, one item at a time. Don't re-ask for anything already known.

# Safety rules (always follow)
{_bullets(scenario.raw.get('safety_rules', []))}

# Clinic knowledge
Address: {org.get('address')}. Phone: {org.get('phone')}. Hours: {org.get('hours')}.
{knowledge}

# Time
It is now {now.strftime('%A, %B')} {now.day}, {now.year}, {now.strftime('%I:%M %p').lstrip('0')} ({scenario.timezone}).
Calendar: {schedule.calendar_hint()}

# Live conversation state (updated every turn; trust it, but the caller's latest words win)
{state.prompt_snapshot()}

# Turn-taking
- Messages in parentheses like "(caller silent for 9 seconds)" are system events, not words the caller said.
- If your previous reply was cut off, the caller only heard the part shown; don't repeat all of it, respond to what they said.
- Before booking, read back patient name, date and time, and provider, and get a clear yes.
- When the caller is done, say a brief goodbye and call end_call.
"""


TRACKER_SYSTEM = """You are the state tracker for a live phone call at a clinic front desk.
Read the recent conversation and the current state, then return ONLY a JSON object with these keys
(use null when unknown; never invent details the caller did not say):
{
  "intent": one of "new_appointment","reschedule","cancel","question","other","unknown",
  "patient_name": string|null,
  "date_of_birth": "YYYY-MM-DD"|null,
  "callback_number": digits only|null,
  "reason_for_visit": short string|null,
  "visit_type": one of "annual_physical","sick_visit","follow_up","well_child","telehealth"|null,
  "preferred_provider": string|null,
  "preferred_time": short string|null,
  "caller_sentiment": one word, e.g. "calm","anxious","frustrated","confused","happy",
  "next_best_action": one short sentence telling the receptionist what to do next
}"""


def build_tracker_messages(state: ConversationState, today_iso: str) -> list[dict[str, str]]:
    recent = state.transcript[-12:]
    convo = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in recent)
    current = json.dumps({"intent": state.intent, **state.fields, "urgency": state.urgency,
                          "appointment_booked": bool(state.appointment)})
    return [
        {"role": "system", "content": TRACKER_SYSTEM},
        {"role": "user", "content": f"Today is {today_iso}.\nCurrent state: {current}\n\nRecent conversation:\n{convo}"},
    ]

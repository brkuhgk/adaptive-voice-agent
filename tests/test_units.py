import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.events import EventBus
from app.scenario import load_scenario
from app.scheduling import ClinicSchedule, normalize_dob
from app.sentence import SentenceChunker
from app.state import ConversationState
from app.tools import ToolExecutor

TZ = "America/Chicago"
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo(TZ))  # a Monday morning


def stream_chunks(text: str, step: int = 3) -> list[str]:
    ch = SentenceChunker()
    out = []
    for i in range(0, len(text), step):
        out += ch.push(text[i:i + step])
    return out + ch.flush()


# ------------------------------------------------------------------ sentences
def test_chunker_respects_abbreviations_and_times():
    text = "Sure! Dr. Reyes has 2:30 p.m. open on Tuesday. Does that work? Great."
    assert stream_chunks(text) == ["Sure!", "Dr. Reyes has 2:30 p.m. open on Tuesday.", "Does that work?", "Great."]


def test_chunker_soft_splits_long_first_clause_and_strips_markdown():
    text = "Okay so I found a few openings for you, **Tuesday morning** at nine or ten, and Wednesday at two"
    chunks = stream_chunks(text, step=5)
    assert len(chunks) >= 2
    assert all("*" not in c for c in chunks)
    assert " ".join(chunks).startswith("Okay so I found a few openings for you,")


def test_chunker_keeps_decimals():
    assert stream_chunks("The fee is 3.50 dollars. Thanks.") == ["The fee is 3.50 dollars.", "Thanks."]


# ------------------------------------------------------------------ schedule + tools
@pytest.fixture
def scenario():
    return load_scenario("clinic")


@pytest.fixture
def schedule(scenario, monkeypatch):
    sched = ClinicSchedule.build(TZ, scenario.raw["demo_records"], now=NOW)
    monkeypatch.setattr(sched, "now", lambda: NOW)
    return sched


@pytest.fixture
def state(scenario):
    return ConversationState("CAtest", list(scenario.required_fields), list(scenario.optional_fields))


def test_dob_normalization():
    assert normalize_dob("April 12th, 1998") == "1998-04-12"
    assert normalize_dob("04/12/1998") == "1998-04-12"
    assert normalize_dob("nonsense") is None


def test_availability_filters_age_and_time(schedule):
    res = schedule.check_availability("sick_visit", patient_age_group="adult", time_of_day="afternoon")
    assert res["available"]
    for s in res["available"]:
        assert "Okafor" not in s["description"]
        assert datetime.fromisoformat(s["start_iso"]).hour >= 12


def test_availability_on_closed_day_returns_next_with_note(schedule):
    res = schedule.check_availability("annual_physical", date_str="2026-10-10")  # Saturday
    assert res["available"] and "note" in res


def test_tool_flow_books_and_updates_state(schedule, state):
    tools = ToolExecutor(schedule, state)
    avail = tools.execute("check_availability", json.dumps({"visit_type": "annual_physical"}))
    slot = avail["available"][0]["slot_id"]
    assert state.offered_slots and state.phase in {"identify_need", "collecting_details"}

    booked = tools.execute("book_appointment", json.dumps({
        "slot_id": slot, "patient_name": "Alex Kim", "date_of_birth": "1990-01-02",
        "visit_type": "annual_physical", "reason_for_visit": "yearly physical", "callback_number": "2105550199"}))
    assert booked["ok"] and state.appointment and state.phase == "confirmed"

    again = tools.execute("book_appointment", json.dumps({
        "slot_id": slot, "patient_name": "Someone Else", "date_of_birth": "1990-01-02", "visit_type": "annual_physical"}))
    assert not again["ok"]  # no double booking


def test_lookup_requires_dob_then_cancel_frees_slot(schedule, state):
    tools = ToolExecutor(schedule, state)
    partial = tools.execute("lookup_appointment", json.dumps({"patient_name": "Jordan Lee"}))
    assert partial["needs_verification"] and "appointments" not in partial
    full = tools.execute("lookup_appointment", json.dumps({"patient_name": "jordan lee", "date_of_birth": "1998-04-12"}))
    appt_id = full["appointments"][0]["appointment_id"]
    slot_id = schedule.appointments[appt_id].slot_id
    assert schedule.slots[slot_id].booked
    assert tools.execute("cancel_appointment", json.dumps({"appointment_id": appt_id}))["ok"]
    assert not schedule.slots[slot_id].booked


def test_urgency_escalates_never_downgrades_and_end_call(schedule, state):
    tools = ToolExecutor(schedule, state)
    tools.execute("flag_urgent", json.dumps({"level": "emergency", "reason": "chest pain"}))
    tools.execute("flag_urgent", json.dumps({"level": "same_day", "reason": "fever"}))
    assert state.urgency == "emergency" and state.phase == "emergency"
    tools.execute("end_call", json.dumps({"reason": "emergency"}))
    assert state.end_requested


def test_bad_tool_args_do_not_crash(schedule, state):
    tools = ToolExecutor(schedule, state)
    assert "error" in tools.execute("book_appointment", "{not json")
    assert "error" in tools.execute("book_appointment", json.dumps({"slot_id": "S1"}))
    assert "error" in tools.execute("no_such_tool", "{}")


def test_tracker_merge_only_overwrites_with_real_values(state):
    changed = state.merge_tracker_update({"intent": "new_appointment", "patient_name": "Ana Diaz",
                                          "date_of_birth": None, "callback_number": "null", "caller_sentiment": "anxious"})
    assert set(changed) >= {"intent", "patient_name", "caller_sentiment"}
    assert state.fields["callback_number"] is None
    assert state.phase == "collecting_details"
    assert "date_of_birth" in state.missing_required
    state.merge_tracker_update({"intent": "unknown"})
    assert state.intent == "new_appointment"


def test_event_bus_replay_skips_deltas():
    bus = EventBus()
    bus.publish("c1", "agent_delta", text="hi")
    bus.publish("c1", "agent_turn", text="hi there")
    assert [e["type"] for e in bus.recent()] == ["agent_turn"]


def test_llm_settings_presets(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "github")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    s = Settings()
    assert s.resolved_llm_base_url == "https://models.github.ai/inference"
    assert s.resolved_llm_model == "openai/gpt-4.1-mini"

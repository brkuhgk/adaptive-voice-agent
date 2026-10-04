import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent import FALLBACK_REPLY, Agent
from app.config import Settings
from app.events import EventBus
from app.llm import ScriptedLLM
from app.profiles import ClinicProfile
from app.scenario import load_scenario
from app.scheduling import ClinicSchedule

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("America/Chicago"))


def make_agent(llm, tracker=True):
    scenario = load_scenario("clinic")
    schedule = ClinicSchedule.build(scenario.timezone, scenario.raw["demo_records"], now=NOW)
    profile = ClinicProfile(scenario, schedule)
    state = profile.new_state("CAagent", None)
    settings = Settings()
    settings.state_tracker_enabled = tracker
    bus = EventBus()
    agent = Agent(profile, state, llm, bus, settings)
    agent.add_assistant_text(profile.opening_line)
    return agent, bus


def assert_history_valid(messages):
    """Every assistant tool_call message must be followed by matching tool results."""
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"]]
            following = [x["tool_call_id"] for x in messages[i + 1:i + 1 + len(ids)] if x["role"] == "tool"]
            assert following == ids, f"tool results missing after message {i}"
        if m["role"] == "assistant" and not m.get("tool_calls"):
            assert m["content"], "empty assistant message"


async def collect(gen):
    return "".join([p async for p in gen])


async def test_plain_turn_and_state_tracker_runs_in_parallel():
    llm = ScriptedLLM([["Oh no, sorry to hear that. What's the patient's full name?"]],
                      tracker=[{"intent": "new_appointment", "reason_for_visit": "sore throat",
                                "caller_sentiment": "tired", "next_best_action": "Ask for the patient's name."}])
    agent, bus = make_agent(llm)
    reply = await collect(agent.respond("Hi, I've had a sore throat for three days and want to come in."))
    await agent._tracker_task
    assert "full name" in reply
    assert agent.state.intent == "new_appointment"
    assert agent.state.fields["reason_for_visit"] == "sore throat"
    # The system prompt for the reply carries the live state + context.
    system = llm.calls[0][0]["content"]
    assert "Riverbend Family Clinic" in system and "still needed (required)" in system
    assert any(e["type"] == "state" and e["source"] == "tracker" for e in bus.recent())


async def test_tool_turn_yields_filler_then_grounded_answer():
    llm = ScriptedLLM([[{"tool": "check_availability", "args": {"visit_type": "sick_visit", "patient_age_group": "adult"}},
                        "I have Monday at ten thirty with Priya Shah. Would that work?"]])
    agent, bus = make_agent(llm, tracker=False)
    pieces = [p async for p in agent.respond("Can I get in soon?")]
    filler = pieces[0].strip()
    assert filler in agent.profile.tool_fillers["check_availability"]
    assert agent.state.offered_slots
    # the second LLM round saw the tool result, and knows the filler was already spoken
    assert llm.calls[1][-1]["role"] == "tool"
    assert llm.calls[1][-2]["tool_calls"] and llm.calls[1][-2]["content"] == filler
    assert agent.messages[-1]["content"].startswith("I have Monday")  # no duplicated filler in history
    assert agent.state.transcript[-1]["text"].startswith(filler)     # but the transcript has everything said
    assert_history_valid(agent.messages)
    assert any(e["type"] == "tool_call" and e["name"] == "check_availability" for e in bus.recent())


async def test_end_call_without_goodbye_speaks_closing_line():
    llm = ScriptedLLM([[{"tool": "end_call", "args": {"reason": "done"}}, ""]])
    agent, _ = make_agent(llm, tracker=False)
    reply = await collect(agent.respond("That's all, thanks."))
    assert agent.state.end_requested
    assert agent.profile.closing_line in reply


async def test_llm_failure_falls_back_gracefully():
    class Boom(ScriptedLLM):
        async def stream_chat(self, messages, tools=None):
            raise RuntimeError("503 from provider")
            yield  # pragma: no cover

    agent, _ = make_agent(Boom([]), tracker=False)
    assert await collect(agent.respond("hello?")) == FALLBACK_REPLY
    assert_history_valid(agent.messages)


async def test_cancel_mid_stream_keeps_history_valid_and_interruption_is_recorded():
    llm = ScriptedLLM([["Let me tell you all about our very long list of parking options and directions and more."]],
                      token_delay=0.02)
    agent, _ = make_agent(llm, tracker=False)

    async def consume():
        async for _ in agent.respond("Where do I park?"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.09)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    agent.record_interruption("Let me tell you all about")
    assert agent.messages[-1]["content"].endswith("[caller interrupted here]")
    assert agent.state.transcript[-1]["interrupted"] is True
    assert_history_valid(agent.messages)


async def test_retract_merges_split_turn():
    llm = ScriptedLLM([["Sure."]], token_delay=0.05)
    agent, _ = make_agent(llm, tracker=False)
    task = asyncio.create_task(collect(agent.respond("I need to book")))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.retract_last_turn() == "I need to book"
    assert agent.messages[-1]["role"] == "assistant"  # back to the opening line
    assert [t["role"] for t in agent.state.transcript] == ["agent"]

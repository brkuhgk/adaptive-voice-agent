"""Chat with the agent in your terminal (real LLM, no phone/STT/TTS).

    python -m scripts.simulate            # type as the caller, Ctrl+C to quit
    python -m scripts.simulate --state    # also print the conversation state after each turn
"""
from __future__ import annotations

import argparse
import asyncio
import json

from app.config import settings
from app.events import EventBus
from app.llm import make_llm
from app.profiles import ClinicProfile
from app.scenario import load_scenario
from app.scheduling import ClinicSchedule
from app.text_session import TextSession

DIM, BOLD, CYAN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[0m"


async def main(show_state: bool) -> None:
    scenario = load_scenario(settings.scenario)
    schedule = ClinicSchedule.build(scenario.timezone, scenario.raw.get("demo_records"))
    bus = EventBus()
    q = bus.subscribe()
    session = TextSession(ClinicProfile(scenario, schedule), make_llm(settings), bus, settings)
    name = scenario.agent["name"]
    print(f"{DIM}LLM: {settings.llm_provider} / {settings.resolved_llm_model}{RESET}")
    print(f"{BOLD}{CYAN}{name}:{RESET} {session.opening_line}")
    while not session.ended:
        try:
            text = await asyncio.to_thread(input, f"{BOLD}You:{RESET} ")
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip():
            continue
        reply = await session.say(text)
        while not q.empty():
            e = q.get_nowait()
            if e["type"] == "tool_call":
                print(f"{YELLOW}  ⚙ {e['name']}({json.dumps(e['args'])}){RESET}")
        print(f"{BOLD}{CYAN}{name}:{RESET} {reply}")
        if show_state:
            s = session.state.to_dict()
            print(f"{DIM}  phase={s['phase']} intent={s['intent']} urgency={s['urgency']} "
                  f"missing={s['missing_required']}\n  plan={s['next_best_action']}{RESET}")
    print(f"{DIM}(call ended){RESET}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", action="store_true")
    asyncio.run(main(ap.parse_args().state))

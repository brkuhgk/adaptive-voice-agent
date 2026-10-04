"""Offline demo of the security-awareness training brain. No API keys, no phone, no cost.

Runs a scripted vishing *drill* (a transcript of an authorized simulation) through the
analyzer and prints the personalized training plan the agent would assign at the debrief.

    python -m scripts.training_report            # the "fell for it" sample
    python -m scripts.training_report --resisted # the "verified and refused" sample

Use it to rehearse the debrief and to show on the projector what the agent produces.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.training import DrillResult, build_report

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "security_awareness.json"


# A scripted drill: (speaker, line, signal). `signal` is what the agent would log for that
# turn — a vulnerability ("vuln", tactic, detail) or a good behaviour ("pass", description).
# The link is always the benign training URL; no real secret is ever spoken or stored.
COMPLIED_TRANSCRIPT = [
    ("riley", "Hi, this is Riley from the IT Service Desk. We flagged a sign-in on your account "
              "from a new device and I need thirty seconds to clear it before it locks you out. Is now okay?", None),
    ("you",   "Oh no, yeah, okay, what do I need to do?", ("vuln", "urgency", "acted on the lock-out deadline without verifying")),
    ("riley", "No worries, happens all the time. I'm on the service desk, I can see the alert on my end right now.", None),
    ("you",   "Okay, sure.", ("vuln", "authority", "accepted the IT-support claim without asking for a ticket or calling back")),
    ("riley", "I'm sending a link to confirm it's really you — training.northwind.example/verify. Can you open that?", None),
    ("you",   "Yeah, I'm clicking it now.", ("vuln", "curiosity_link", "agreed to open a link sent during the call")),
    ("riley", "Perfect. It'll show a six-digit code. Just read me the code so I can match it.", None),
    ("you",   "Okay it says four—", ("vuln", "credential_harvest", "began reading out a one-time code (STOPPED by the agent)")),
    # <-- teachable moment fires here: the agent interrupts BEFORE any real secret is captured.
]

RESISTED_TRANSCRIPT = [
    ("riley", "Hi, this is Riley from the IT Service Desk. We flagged a sign-in from a new device "
              "and I need thirty seconds to clear it before it locks you out. Is now okay?", None),
    ("you",   "What's your name and a ticket number? I'll call the service desk back on the number on our intranet.",
              ("pass", "offered to hang up and call the official help-desk number back")),
    ("riley", "Oh, no need, I can just do it now — it's faster before the lock kicks in.", None),
    ("you",   "I don't share codes or click links from inbound calls. I'll verify through the portal.",
              ("pass", "refused to share codes or click links from an unexpected call")),
    ("you",   "And I'm going to report this call to security either way.",
              ("pass", "committed to reporting the suspicious call")),
]


def run(transcript: list[tuple[str, str, tuple | None]], participant: str) -> None:
    modules = json.loads(SCENARIO.read_text())["knowledge"]["training_modules"]
    result = DrillResult(participant=participant)

    print(f"\n{'='*70}\n  AUTHORIZED VISHING DRILL — simulated transcript\n{'='*70}")
    for speaker, line, signal in transcript:
        who = "RILEY (sim)" if speaker == "riley" else "YOU"
        print(f"\n  {who}: {line}")
        if signal and signal[0] == "vuln":
            _, tactic, detail = signal
            sig = result.log_vulnerability(tactic, detail)
            print(f"     └─ [logged] lever: {tactic} (severity {sig.severity}) — {detail}")
        elif signal and signal[0] == "pass":
            _, behavior = signal
            result.pass_check(behavior)
            print(f"     └─ [logged] good behaviour — {behavior}")

    report = build_report(result, modules)

    print(f"\n{'='*70}\n  TEACHABLE-MOMENT DEBRIEF\n{'='*70}")
    print(f"\n  Outcome:     {report['outcome'].upper()}")
    print(f"  Risk level:  {report['risk_band'].upper()}  (score {report['score']})")
    print(f"               {report['risk_summary']}")

    if report["levers_that_worked"]:
        print("\n  What worked on you:")
        for lv in report["levers_that_worked"]:
            print(f"    • {lv['tactic']}: {lv['detail']}")
    if report["did_well"]:
        print("\n  What you did well:")
        for g in report["did_well"]:
            print(f"    ✓ {g}")

    print("\n  Your training plan:")
    if not report["training_plan"]:
        print("    (nothing assigned)")
    for i, p in enumerate(report["training_plan"], 1):
        mins = f"{p['minutes']} min" if p.get("minutes") else ""
        print(f"    {i}. [{p['priority'].upper():6}] {p['title']}  {mins}")
        print(f"       {p['covers']}")
        print(f"       addresses: {', '.join(p['because_of'])}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline security-awareness training-report demo.")
    ap.add_argument("--resisted", action="store_true", help="Run the 'verified and refused' sample instead.")
    args = ap.parse_args()
    if args.resisted:
        run(RESISTED_TRANSCRIPT, "Sam (resisted)")
    else:
        run(COMPLIED_TRANSCRIPT, "Alex (fell for it)")


if __name__ == "__main__":
    main()

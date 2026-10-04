"""Tests for the security-awareness training analyzer and the scenario it drives."""
from __future__ import annotations

import json
from pathlib import Path

from app.training import DrillResult, build_report, recommend_training

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "security_awareness.json"


def _modules() -> dict:
    return json.loads(SCENARIO.read_text())["knowledge"]["training_modules"]


def test_scenario_is_valid_and_self_consistent():
    raw = json.loads(SCENARIO.read_text())
    assert raw["id"] == "security_awareness"
    # The consent/limits must be present — this is what makes the drill authorized, not an attack.
    assert "authorization_required" in raw["simulation"]
    assert any("SIMULATION" in h or "simulation" in h for h in raw["simulation"]["hard_limits"])
    # Every training module declares which tactics it addresses, and they're all known tactics.
    tactics = set(raw["knowledge"]["tactics"])
    for mod in raw["knowledge"]["training_modules"].values():
        assert set(mod["addresses"]).issubset(tactics)


def test_resisted_drill_scores_low_and_assigns_only_the_baseline():
    result = DrillResult(participant="Sam")
    result.pass_check("offered to call the help desk back")
    result.pass_check("refused to share any codes")
    report = build_report(result, _modules())

    assert report["outcome"] == "resisted"
    assert report["risk_band"] == "low"
    assert report["score"] == 0
    # No vulnerabilities -> the only thing assigned is the universal "report it" module.
    plan = report["training_plan"]
    assert [p["module"] for p in plan] == ["report_it"]


def test_complied_drill_is_high_risk_and_prioritizes_the_worst_lever():
    result = DrillResult(participant="Alex")
    result.log_vulnerability("urgency", "acted on the deadline")
    result.log_vulnerability("authority", "trusted the IT claim")
    result.log_vulnerability("curiosity_link", "clicked the link")
    result.log_vulnerability("credential_harvest", "began reading a code")
    report = build_report(result, _modules())

    assert report["outcome"] == "complied"
    assert report["risk_band"] in {"elevated", "high"}
    plan = report["training_plan"]
    # The highest-severity lever (credential_harvest, 5) must surface its module first.
    assert plan[0]["module"] == "never_share_codes"
    assert plan[0]["priority"] == "high"
    # The baseline reporting module is always included, at low priority.
    assert any(p["module"] == "report_it" and p["priority"] == "low" for p in plan)


def test_plan_is_deduplicated_across_tactics_mapping_to_one_module():
    # urgency and fear both map to the same "pressure_tactics" module; it must appear once.
    result = DrillResult()
    result.log_vulnerability("urgency", "deadline")
    result.log_vulnerability("fear", "threat of lockout")
    plan = recommend_training(result, _modules())
    pressure = [p for p in plan if p["module"] == "pressure_tactics"]
    assert len(pressure) == 1
    assert set(pressure[0]["because_of"]) == {"urgency", "fear"}


def test_good_behaviors_blunt_but_do_not_erase_the_score():
    result = DrillResult()
    result.log_vulnerability("curiosity_link", "clicked")  # severity 4
    for b in ("called back", "asked for ticket", "reported it", "used a manager"):
        result.pass_check(b)
    # Credit is capped at 3, so 4 - 3 = 1; never negative.
    assert result.score == 1


def test_signal_never_needs_a_real_secret():
    # A detail string is a description of the behaviour, never the secret itself.
    result = DrillResult()
    sig = result.log_vulnerability("credential_harvest", "began reading out a one-time code (stopped)")
    assert "code" in sig.detail.lower()
    assert sig.severity == 5

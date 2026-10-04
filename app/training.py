"""Security-awareness training analysis.

The brain of the `security_awareness` scenario. Given the social-engineering levers a
participant responded to during an *authorized* vishing drill (and the good behaviours they
showed), score their susceptibility and build a short, prioritized training plan.

This is deliberately pure, deterministic logic with no LLM and no telephony: the live voice
agent calls `log_vulnerability` / `pass_check` / `recommend_training` as tools (see the
training tool executor), and the same functions here power the offline report and the tests.

Nothing in here ever stores a real secret. A "signal" records only *which lever* the person
responded to (e.g. "agreed to click the link"), never the content of any code or password.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# How dangerous it is that the person responded to a given social-engineering lever.
# Higher = more likely to lead to a real compromise.
TACTIC_SEVERITY: dict[str, int] = {
    "credential_harvest": 5,   # started to share a password / PIN / OTP
    "remote_access": 5,        # willing to install software or share a remote-access code
    "curiosity_link": 4,       # agreed to click/visit the link
    "authority": 3,            # deferred to the "IT" authority without checking
    "pretext": 3,              # accepted the fabricated story at face value
    "fear": 2,                 # moved by the threat of a locked account / trouble
    "urgency": 2,              # moved by the time pressure
    "familiarity": 2,          # trust built purely from a friendly, routine tone
}

# Which training module addresses which lever. Keep in sync with the scenario's
# knowledge.training_modules[...].addresses, but defined here so the analyzer is self-contained.
TACTIC_TO_MODULE: dict[str, str] = {
    "credential_harvest": "never_share_codes",
    "remote_access": "remote_access_redflags",
    "curiosity_link": "link_safety",
    "urgency": "pressure_tactics",
    "fear": "pressure_tactics",
    "authority": "verify_callback",
    "pretext": "verify_callback",
    "familiarity": "verify_callback",
}

RISK_BANDS = (
    (0, "low", "Strong instincts. You resisted the drill with little that needs work."),
    (5, "moderate", "Mostly solid, with a couple of habits worth tightening."),
    (10, "elevated", "Several levers worked on you. A short plan will close most of the gap fast."),
    (16, "high", "This call would likely have succeeded for real. Start the plan this week."),
)


@dataclass
class Signal:
    """One observed moment in the drill: a lever the person responded to."""
    tactic: str
    detail: str = ""           # e.g. "agreed to open the link" — never a real secret
    severity: int = 0

    def __post_init__(self) -> None:
        if not self.severity:
            self.severity = TACTIC_SEVERITY.get(self.tactic, 1)


@dataclass
class DrillResult:
    participant: str = "participant"
    vulnerabilities: list[Signal] = field(default_factory=list)
    good_behaviors: list[str] = field(default_factory=list)

    def log_vulnerability(self, tactic: str, detail: str = "", severity: int = 0) -> Signal:
        sig = Signal(tactic=tactic, detail=detail, severity=severity)
        self.vulnerabilities.append(sig)
        return sig

    def pass_check(self, behavior: str) -> None:
        if behavior not in self.good_behaviors:
            self.good_behaviors.append(behavior)

    # ------------------------------------------------------------------ scoring
    @property
    def score(self) -> int:
        """Total susceptibility score: higher = more at risk. Good behaviours blunt it slightly."""
        raw = sum(s.severity for s in self.vulnerabilities)
        offset = min(len(self.good_behaviors), 3)  # earned resistance caps the credit
        return max(0, raw - offset)

    @property
    def risk_band(self) -> tuple[str, str]:
        label, blurb = RISK_BANDS[0][1], RISK_BANDS[0][2]
        for threshold, lbl, txt in RISK_BANDS:
            if self.score >= threshold:
                label, blurb = lbl, txt
        return label, blurb

    @property
    def outcome(self) -> str:
        if any(s.severity >= 4 for s in self.vulnerabilities):
            return "complied"
        if self.vulnerabilities:
            return "partial"
        return "resisted"


def recommend_training(result: DrillResult, modules: dict[str, Any]) -> list[dict[str, Any]]:
    """Map observed vulnerabilities to a prioritized, de-duplicated training plan.

    `modules` is the scenario's knowledge.training_modules dict. Returns one entry per
    recommended module, highest priority first, each carrying the levers it addresses.
    """
    # Aggregate each module's weight from the severity of the levers that pointed to it.
    weight: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    for sig in result.vulnerabilities:
        mod_key = TACTIC_TO_MODULE.get(sig.tactic)
        if not mod_key:
            continue
        weight[mod_key] = weight.get(mod_key, 0) + sig.severity
        reasons.setdefault(mod_key, []).append(sig.tactic)

    plan: list[dict[str, Any]] = []
    for mod_key, w in sorted(weight.items(), key=lambda kv: (-kv[1], kv[0])):
        mod = modules.get(mod_key, {})
        plan.append({
            "module": mod_key,
            "title": mod.get("title", mod_key),
            "minutes": mod.get("minutes"),
            "covers": mod.get("covers", ""),
            "priority": "high" if w >= 4 else "medium",
            "because_of": sorted(set(reasons[mod_key])),
        })

    # Always close with the reporting habit — it protects the whole org regardless of outcome.
    if "report_it" in modules and not any(p["module"] == "report_it" for p in plan):
        mod = modules["report_it"]
        plan.append({
            "module": "report_it",
            "title": mod.get("title", "report_it"),
            "minutes": mod.get("minutes"),
            "covers": mod.get("covers", ""),
            "priority": "low",
            "because_of": ["baseline"],
        })
    return plan


def build_report(result: DrillResult, modules: dict[str, Any]) -> dict[str, Any]:
    """Full, serializable debrief for the dashboard, an email, or the offline script."""
    band, blurb = result.risk_band
    return {
        "participant": result.participant,
        "outcome": result.outcome,
        "score": result.score,
        "risk_band": band,
        "risk_summary": blurb,
        "levers_that_worked": [
            {"tactic": s.tactic, "detail": s.detail, "severity": s.severity} for s in result.vulnerabilities
        ],
        "did_well": result.good_behaviors,
        "training_plan": recommend_training(result, modules),
    }

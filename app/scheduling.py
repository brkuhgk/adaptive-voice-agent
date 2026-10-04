"""A small in-memory clinic schedule so tool calls return grounded, realistic data.

Availability is generated relative to "today", so the demo always has open slots.
Bookings live in memory for the lifetime of the server process.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

PROVIDER_VISIT_TYPES = {
    "Dr. Ana Reyes": {"annual_physical", "follow_up", "sick_visit"},
    "Dr. James Okafor": {"well_child", "sick_visit", "follow_up"},
    "Priya Shah, NP": {"sick_visit", "telehealth", "follow_up"},
}

PROVIDER_AGES = {"Dr. Ana Reyes": "adult", "Dr. James Okafor": "child", "Priya Shah, NP": "all"}

DAY_TIMES = [time(h, m) for h in range(8, 17) for m in (0, 30) if not (h == 12) and (h, m) != (8, 0)]


def spoken_time(t: time) -> str:
    hour = t.hour % 12 or 12
    suffix = "a.m." if t.hour < 12 else "p.m."
    return f"{hour}:{t.minute:02d} {suffix}" if t.minute else f"{hour} {suffix}"


def spoken_date(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def normalize_dob(dob: str) -> str | None:
    """Accept '1998-04-12', '04/12/1998', 'April 12, 1998', 'April 12th 1998'."""
    if not dob:
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)", r"\1", dob.strip())
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass
class Slot:
    slot_id: str
    start: datetime
    provider: str
    visit_types: set[str]
    booked: bool = False

    def describe(self, today: date) -> str:
        return f"{spoken_date(self.start.date(), today)} at {spoken_time(self.start.time())} with {self.provider}"


@dataclass
class Appointment:
    appointment_id: str
    patient_name: str
    date_of_birth: str
    visit_type: str
    provider: str
    start: datetime
    reason: str = ""
    callback_number: str = ""
    status: str = "booked"
    slot_id: str | None = None

    def to_dict(self, today: date) -> dict[str, Any]:
        return {
            "appointment_id": self.appointment_id,
            "patient_name": self.patient_name,
            "visit_type": self.visit_type,
            "provider": self.provider,
            "when": f"{spoken_date(self.start.date(), today)} at {spoken_time(self.start.time())}",
            "start_iso": self.start.isoformat(),
            "status": self.status,
        }


@dataclass
class ClinicSchedule:
    tz: ZoneInfo
    seed: int = 7
    days_ahead: int = 10
    slots: dict[str, Slot] = field(default_factory=dict)
    appointments: dict[str, Appointment] = field(default_factory=dict)
    _next_appt: int = 2001
    clock: Callable[[], datetime] | None = None  # fixed clock for tests

    @classmethod
    def build(cls, timezone: str, demo_records: list[dict[str, Any]] | None = None, now: datetime | None = None,
              seed: int = 7) -> "ClinicSchedule":
        tz = ZoneInfo(timezone)
        sched = cls(tz=tz, seed=seed, clock=(lambda: now) if now else None)
        sched._generate(sched.now())
        for rec in demo_records or []:
            sched._seed_record(rec)
        return sched

    # ------------------------------------------------------------------ setup
    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def _generate(self, now: datetime) -> None:
        rng = random.Random(self.seed)
        counter = 100
        d = now.date()
        added_days = 0
        while added_days < self.days_ahead:
            if d.weekday() < 5:
                added_days += 1
                for provider, types in PROVIDER_VISIT_TYPES.items():
                    for t in DAY_TIMES:
                        start = datetime.combine(d, t, tzinfo=self.tz)
                        if start <= now + timedelta(minutes=45):
                            continue
                        # Same-day sick visits are held open in the morning for the NP.
                        held = provider.startswith("Priya") and t.hour < 12 and d == now.date()
                        busy = rng.random() < (0.35 if held else 0.62)
                        counter += 1
                        sid = f"S{counter}"
                        self.slots[sid] = Slot(sid, start, provider, set(types), booked=busy)
            d += timedelta(days=1)

    def _seed_record(self, rec: dict[str, Any]) -> None:
        d = self.today()
        days = int(rec.get("days_from_today", 1))
        target = d
        while days > 0:
            target += timedelta(days=1)
            if target.weekday() < 5:
                days -= 1
        hh, mm = (int(x) for x in rec.get("time", "10:00").split(":"))
        start = datetime.combine(target, time(hh, mm), tzinfo=self.tz)
        appt = Appointment(
            appointment_id=rec["appointment_id"], patient_name=rec["patient_name"],
            date_of_birth=rec["date_of_birth"], visit_type=rec["visit_type"], provider=rec["provider"], start=start,
        )
        self.appointments[appt.appointment_id] = appt
        for s in self.slots.values():
            if s.start == start and s.provider == appt.provider:
                s.booked = True
                appt.slot_id = s.slot_id

    # ------------------------------------------------------------------ queries
    def calendar_hint(self, days: int = 10) -> str:
        """Lets the LLM map 'next Tuesday' to an exact date without doing date math."""
        d = self.today()
        out = []
        for i in range(days):
            day = d + timedelta(days=i)
            label = "today" if i == 0 else ("tomorrow" if i == 1 else day.strftime("%A"))
            out.append(f"{label} {day.strftime('%a %b')} {day.day} = {day.isoformat()}")
        return "; ".join(out)

    def check_availability(self, visit_type: str, provider: str | None = None, date_str: str | None = None,
                           time_of_day: str | None = None, patient_age_group: str | None = None,
                           limit: int = 3) -> dict[str, Any]:
        visit_type = (visit_type or "").strip().lower() or "sick_visit"
        now = self.now()
        today = now.date()
        want_date: date | None = None
        if date_str:
            try:
                want_date = date.fromisoformat(date_str[:10])
            except ValueError:
                want_date = None

        def ok(s: Slot) -> bool:
            if s.booked or visit_type not in s.visit_types or s.start <= now:
                return False
            if provider and normalize_name(provider).split()[-1] not in normalize_name(s.provider):
                return False
            if patient_age_group in {"adult", "child"} and PROVIDER_AGES.get(s.provider, "all") not in {"all", patient_age_group}:
                return False
            if time_of_day == "morning" and s.start.hour >= 12:
                return False
            if time_of_day == "afternoon" and s.start.hour < 12:
                return False
            return True

        candidates = sorted((s for s in self.slots.values() if ok(s)), key=lambda s: s.start)
        note = None
        if want_date:
            same_day = [s for s in candidates if s.start.date() == want_date]
            if same_day:
                candidates = same_day
            else:
                later = [s for s in candidates if s.start.date() > want_date]
                note = f"No openings on {spoken_date(want_date, today)}; these are the next closest."
                candidates = later or candidates
        # Spread suggestions out a little instead of three back-to-back slots.
        picked: list[Slot] = []
        for s in candidates:
            if all(abs((s.start - p.start).total_seconds()) >= 3600 or s.provider != p.provider for p in picked):
                picked.append(s)
            if len(picked) >= limit:
                break
        if not picked:
            return {"available": [], "note": "No openings in the next two weeks for that request."}
        return {
            "visit_type": visit_type,
            "available": [{"slot_id": s.slot_id, "description": s.describe(today), "start_iso": s.start.isoformat()}
                          for s in picked],
            **({"note": note} if note else {}),
        }

    def book(self, slot_id: str, patient_name: str, date_of_birth: str, visit_type: str, reason: str = "",
             callback_number: str = "", reschedule_from: str | None = None) -> dict[str, Any]:
        slot = self.slots.get((slot_id or "").strip().upper())
        if slot is None:
            return {"ok": False, "error": f"Unknown slot_id {slot_id}. Call check_availability again."}
        if slot.booked:
            return {"ok": False, "error": "That slot was just taken. Call check_availability for new options."}
        if visit_type and visit_type not in slot.visit_types:
            return {"ok": False, "error": f"{slot.provider} does not offer {visit_type} in that slot."}
        if reschedule_from:
            self.cancel(reschedule_from)
        slot.booked = True
        appt_id = f"A-{self._next_appt}"
        self._next_appt += 1
        appt = Appointment(
            appointment_id=appt_id, patient_name=patient_name.strip(),
            date_of_birth=normalize_dob(date_of_birth) or date_of_birth, visit_type=visit_type or "sick_visit",
            provider=slot.provider, start=slot.start, reason=reason, callback_number=callback_number, slot_id=slot.slot_id,
        )
        self.appointments[appt_id] = appt
        return {"ok": True, "appointment": appt.to_dict(self.today()),
                "say": f"Booked {slot.describe(self.today())}."}

    def lookup(self, patient_name: str, date_of_birth: str | None = None) -> dict[str, Any]:
        name = normalize_name(patient_name)
        dob = normalize_dob(date_of_birth or "")
        matches = []
        for a in self.appointments.values():
            if a.status != "booked":
                continue
            a_name = normalize_name(a.patient_name)
            name_ok = name and (name == a_name or name.split()[-1] == a_name.split()[-1])
            if name_ok and (dob is None or dob == a.date_of_birth):
                matches.append(a)
        if not matches:
            return {"found": False, "note": "No upcoming appointment matches that name and date of birth."}
        if dob is None:
            return {"found": True, "needs_verification": True,
                    "note": "Found a possible match. Confirm date of birth before sharing details."}
        return {"found": True, "appointments": [a.to_dict(self.today()) for a in matches]}

    def cancel(self, appointment_id: str) -> dict[str, Any]:
        appt = self.appointments.get((appointment_id or "").strip().upper())
        if appt is None or appt.status != "booked":
            return {"ok": False, "error": "No active appointment with that id."}
        appt.status = "cancelled"
        if appt.slot_id and appt.slot_id in self.slots:
            self.slots[appt.slot_id].booked = False
        return {"ok": True, "cancelled": appt.to_dict(self.today())}

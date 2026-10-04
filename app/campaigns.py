"""Outbound call campaigns.

A campaign is one folder. You edit four files; the code reads them at the start of each call:

    campaigns/<name>/
        campaign.json     settings: agent name, voice, goal, opening line, what to collect, calling hours
        instructions.md   how the agent must behave (your instructions to Maya)
        content.md        facts the agent may use when it talks
        contacts.csv      who to call: phone, name, consent, plus any extra columns you want

With "supabase_contacts" in campaign.json, people from the Supabase signup_requests table are added to
the list too (see app/supabase_db.py). A number already in contacts.csv keeps its CSV row.

Results go to campaigns/<name>/results.csv (one row per contact) and campaigns/<name>/transcripts/.
People added from the dashboard go to <data dir>/<name>/contacts_added.csv (same columns as contacts.csv).
Numbers that ask not to be called again go to campaigns/do_not_call.txt (shared by all campaigns).
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import supabase_db
from .profiles import AgentProfile
from .state import ConversationState

log = logging.getLogger(__name__)

CAMPAIGN_DIR = Path(os.getenv("CAMPAIGN_DIR", Path(__file__).resolve().parent.parent / "campaigns"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
TODO_RE = re.compile(r"\bTODO\b")


def data_root() -> Path:
    """Where results, transcripts and the do-not-call list live. In the cloud this is a volume
    (DATA_DIR=/data), so redeploying new campaign files never wipes your results."""
    env = os.getenv("DATA_DIR")
    return Path(env) if env else CAMPAIGN_DIR
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
YES = {"yes", "y", "true", "1", "x", "consented"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_OUTCOMES = ["completed", "not_interested", "callback_requested", "wrong_person", "opted_out"]
# Outcomes the system sets itself (not chosen by the LLM).
SYSTEM_OUTCOMES = ["voicemail_left", "machine_no_message", "hung_up", "no_conversation"]
RETRY_OUTCOMES = {"", "voicemail_left", "machine_no_message", "no_conversation"}
AI_DISCLOSURE = re.compile(r"\b(AI|A\.I\.|artificial|virtual assistant|automated assistant)\b")
RESERVED_COLUMNS = {"phone", "consent", "timezone", "voice_id", "id", "email",
                    "signup_request_id", "status", "consented_at", "created_at"}
ADDED_CONTACTS = "contacts_added.csv"
VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{20}$")  # ElevenLabs voice IDs


def _fill(template: str, values: dict[str, Any]) -> str:
    """Replace {placeholders}; unknown placeholders stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template or "")


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


@dataclass
class Contact:
    id: str
    phone: str
    name: str
    consent: bool
    timezone: str | None
    voice_id: str | None
    row: int
    columns: dict[str, str] = field(default_factory=dict)
    added: bool = False  # added from the dashboard, not contacts.csv
    source: str = "csv"  # csv | dashboard | supabase

    @property
    def where(self) -> str:
        if self.source == "supabase":
            return f"Supabase sign-up {self.name or self.phone}"
        return f"added contact {self.name or self.phone}" if self.added else f"contacts.csv row {self.row}"

    @property
    def signup_request_id(self) -> str:
        return self.columns.get("signup_request_id", "")

    @property
    def first_name(self) -> str:
        return (self.name or "").split(" ")[0] or "there"

    def values(self) -> dict[str, str]:
        return {**self.columns, "name": self.name or "there", "first_name": self.first_name}

    def public_details(self) -> dict[str, str]:
        """Columns the agent may see (never consent flags, routing data, or the phone number)."""
        return {k: v for k, v in self.columns.items() if k not in RESERVED_COLUMNS and v}


@dataclass
class Issue:
    level: str  # "error" | "warning"
    message: str


@dataclass
class Campaign:
    name: str
    path: Path
    config: dict[str, Any]
    instructions: str
    content: str
    contacts: list[Contact]
    issues: list[Issue] = field(default_factory=list)

    # ------------------------------------------------------------ settings
    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    @property
    def title(self) -> str:
        return self.get("title", self.name)

    @property
    def agent_name(self) -> str:
        return self.get("agent_name", "Maya")

    @property
    def org_name(self) -> str:
        return self.get("org_name", "")

    @property
    def voice_id(self) -> str | None:
        return self.get("voice_id") or None

    @property
    def outcomes(self) -> list[str]:
        return list(dict.fromkeys([*self.get("outcomes", DEFAULT_OUTCOMES), "opted_out", "wrong_person"]))

    @property
    def collect(self) -> dict[str, dict[str, Any]]:
        out = {}
        for key, spec in (self.get("collect") or {}).items():
            out[key] = spec if isinstance(spec, dict) else {"description": str(spec), "required": True}
        return out

    @property
    def followups(self) -> dict[str, Any]:
        f = self.get("followups") or {}
        return {"channels": [c for c in f.get("channels", ["email", "sms"]) if c in ("email", "sms")],
                "max_per_call": int(f.get("max_per_call", 2)), "options": f.get("options") or {}}

    @property
    def max_concurrent(self) -> int:
        return max(1, int(self.get("max_concurrent_calls", 1)))

    @property
    def max_attempts(self) -> int:
        return max(1, int(self.get("max_attempts", 2)))

    @property
    def hours(self) -> dict[str, str]:
        return {"start": "09:00", "end": "20:00", "timezone": "America/Chicago", "days": "mon-sun",
                **(self.get("calling_hours") or {})}

    def values_for(self, contact: Contact) -> dict[str, str]:
        """Everything a {placeholder} can refer to, for one contact."""
        return {**contact.values(), "agent_name": self.agent_name, "org_name": self.org_name,
                "callback_number": self.get("callback_number", "")}

    @property
    def disclose_ai_in_opening(self) -> bool:
        """When true (the default), an opening line that doesn't mention AI gets a disclosure in front."""
        return bool(self.get("disclose_ai_in_opening", True))

    def opening_for(self, contact: Contact) -> str:
        text = _fill(self.get("opening_line", "Hi {first_name}, this is {agent_name},  "
                                               "calling from {org_name}. Do you have a minute?"),
                     self.values_for(contact))
        if self.disclose_ai_in_opening and not AI_DISCLOSURE.search(text):
            text = f"Hi, this is {self.agent_name},  calling from {self.org_name}. {text}"
        return text

    def voicemail_for(self, contact: Contact) -> str | None:
        msg = self.get("voicemail_message")
        if not msg:
            return None
        return _fill(msg, self.values_for(contact))

    def contact(self, contact_id: str) -> Contact | None:
        return next((c for c in self.contacts if c.id == contact_id), None)

    # ------------------------------------------------------------ rules
    def in_calling_hours(self, contact: Contact, now: datetime | None = None) -> bool:
        h = self.hours
        tz = ZoneInfo(contact.timezone or h["timezone"])
        local = (now or datetime.now(tz)).astimezone(tz)
        start, end = dtime.fromisoformat(h["start"]), dtime.fromisoformat(h["end"])
        return local.strftime("%a").lower()[:3] in _days(h["days"]) and start <= local.time() < end

    def eligibility(self, contact: Contact, dnc: "DoNotCallList", results: "ResultStore",
                    now: datetime | None = None, seen_phones: set[str] | None = None,
                    recall: bool = False, ignore_hours: bool = False) -> tuple[bool, str]:
        """recall=True is a call you start by hand from the dashboard: it calls again even if the contact
        is done or out of attempts. ignore_hours=True also skips calling hours (you tick that on the
        dashboard). Consent and the do-not-call list always apply."""
        if not E164.match(contact.phone):
            return False, "phone is not in +15551234567 format"
        if seen_phones is not None:
            if contact.phone in seen_phones:
                return False, "duplicate phone number in contacts.csv"
            seen_phones.add(contact.phone)
        if not contact.consent:
            return False, "no consent (consent column is not yes)"
        if dnc.contains(contact.phone):
            return False, "on do-not-call list"
        row = results.get(contact.id)
        outcome = row.get("outcome", "") if row else ""
        attempts = int(row.get("attempts") or 0) if row else 0
        if not recall and outcome and outcome not in RETRY_OUTCOMES:
            return False, f"done ({outcome})"
        if not recall and attempts >= self.max_attempts:
            return False, f"max attempts reached ({attempts})"
        if not ignore_hours and not self.in_calling_hours(contact, now):
            return False, "outside calling hours"
        return True, "ready"

    def plan(self, dnc: "DoNotCallList", results: "ResultStore", now: datetime | None = None) -> list[dict[str, Any]]:
        seen: set[str] = set()
        seen_recall: set[str] = set()
        seen_any_time: set[str] = set()
        rows = []
        for c in self.contacts:
            ok, reason = self.eligibility(c, dnc, results, now, seen)
            can_call, call_reason = self.eligibility(c, dnc, results, now, seen_recall, recall=True)
            any_time, _ = self.eligibility(c, dnc, results, now, seen_any_time, recall=True, ignore_hours=True)
            r = results.get(c.id) or {}
            rows.append({"id": c.id, "name": c.name, "phone": mask(c.phone), "eligible": ok, "reason": reason,
                         "can_call": can_call, "call_reason": call_reason, "can_call_any_time": any_time,
                         "added": c.added, "source": c.source, "call_status": r.get("call_status", ""),
                         "outcome": r.get("outcome", ""),
                         "attempts": r.get("attempts", "0"), "note": mask_numbers(r.get("note", "")),
                         "summary": r.get("summary", ""), "followups": r.get("followups", ""),
                         "voice_id": c.voice_id or self.voice_id or ""})
        return rows


def _days(spec: str) -> set[str]:
    out: set[str] = set()
    for part in str(spec).lower().replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            i, j = DAYS.index(a[:3]), DAYS.index(b[:3])
            out.update(DAYS[i:j + 1] if i <= j else DAYS[i:] + DAYS[:j + 1])
        elif part:
            out.add(part[:3])
    return out


def normalize_phone(raw: str) -> str:
    """'(210) 555-0104' -> '+12105550104'. 10-digit numbers are treated as US/Canada (+1)."""
    phone = re.sub(r"[\s().\-]", "", raw or "")
    if re.fullmatch(r"\d{10}", phone):
        return "+1" + phone
    if re.fullmatch(r"1\d{10}", phone):
        return "+" + phone
    return phone


def mask(phone: str) -> str:
    """+12105550123 -> +1••••••0123 (dashboards are often on a projector)."""
    return phone[:2] + "•" * (len(phone) - 6) + phone[-4:] if len(phone) > 6 else phone


def mask_numbers(text: str) -> str:
    """Mask phone numbers inside free text, such as Twilio error messages."""
    return re.sub(r"\+\d{8,15}", lambda m: mask(m.group(0)), text or "")


# ---------------------------------------------------------------- loading
def list_campaigns() -> list[str]:
    if not CAMPAIGN_DIR.exists():
        return []
    return sorted(p.name for p in CAMPAIGN_DIR.iterdir() if (p / "campaign.json").exists())


def load_campaign(name: str) -> Campaign:
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
        raise ValueError("campaign name may use letters, numbers, - and _ only")
    path = CAMPAIGN_DIR / name
    if not (path / "campaign.json").exists():
        raise FileNotFoundError(f"no campaign at {path}/campaign.json")
    issues: list[Issue] = []
    config = json.loads((path / "campaign.json").read_text())
    read = lambda f: _strip_comments((path / f).read_text()) if (path / f).exists() else ""  # noqa: E731
    instructions, content = read("instructions.md"), read("content.md")
    csv_path = path / "contacts.csv"
    contacts = _read_contacts(csv_path)
    source = _supabase_source(config)
    if not csv_path.exists() and source is None:
        issues.append(Issue("error", "contacts.csv is missing"))
    contacts += _read_contacts(data_root() / name / ADDED_CONTACTS, added=True)
    if source is not None:
        contacts += _read_supabase_contacts(source, contacts, issues)

    camp = Campaign(name, path, config, instructions, content, contacts, issues)
    _validate(camp)
    return camp


def _read_contacts(csv_path: Path, added: bool = False) -> list[Contact]:
    contacts: list[Contact] = []
    if not csv_path.exists():
        return contacts
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            phone = normalize_phone(row.get("phone", ""))
            if not phone and not row.get("name"):
                continue
            cid = row.get("id") or (re.sub(r"\D", "", phone) or f"row{i}")
            contacts.append(Contact(
                id=cid, phone=phone, name=row.get("name", ""), consent=row.get("consent", "").lower() in YES,
                timezone=row.get("timezone") or None, voice_id=row.get("voice_id") or None, row=i, columns=row,
                added=added, source="dashboard" if added else "csv",
            ))
    return contacts


def _supabase_source(config: dict[str, Any]) -> dict[str, Any] | None:
    """campaign.json "supabase_contacts": true, or
    {"table": "signup_requests", "skip_status": ["revoked"], "order": "consented_at.asc"}."""
    raw = config.get("supabase_contacts")
    if not raw:
        return None
    spec = raw if isinstance(raw, dict) else {}
    return {"table": spec.get("table", supabase_db.SIGNUPS_TABLE),
            "skip_status": [s.lower() for s in spec.get("skip_status", ["revoked"])],
            "order": spec.get("order", "consented_at.asc")}


def _read_supabase_contacts(source: dict[str, Any], existing: list[Contact], issues: list[Issue]) -> list[Contact]:
    """People from the sign-up table. consent=false or a skipped status (revoked) shows as 'no consent'.
    A phone already in contacts.csv keeps its CSV row, but gets linked to the sign-up."""
    if not supabase_db.enabled():
        issues.append(Issue("warning", "supabase_contacts is on, but SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY "
                                       "are not set: showing contacts.csv only"))
        return []
    try:
        rows = supabase_db.fetch_signups(source["table"], source["order"])
    except supabase_db.SupabaseError as exc:
        issues.append(Issue("warning", f"Supabase: {exc}. Showing contacts.csv only."))
        return []
    by_phone: dict[str, Contact] = {}
    for c in existing:
        by_phone.setdefault(c.phone, c)  # the first row wins, like the dialer's duplicate check
    out: list[Contact] = []
    for i, r in enumerate(rows, start=1):
        cols = {str(k).lower(): "" if v is None else str(v).strip() for k, v in r.items()}
        phone = normalize_phone(cols.get("phone", ""))
        if not phone and not cols.get("name"):
            continue
        signup_id = cols.pop("id", "")
        cols["signup_request_id"] = signup_id
        match = by_phone.get(phone)
        if match is not None:
            match.columns.setdefault("signup_request_id", signup_id)
            continue
        status = cols.get("status", "").lower()
        consent = cols.get("consent", "").lower() in YES and status not in source["skip_status"]
        contact = Contact(id=re.sub(r"\D", "", phone) or f"sb{i}", phone=phone, name=cols.get("name", ""),
                          consent=consent, timezone=cols.get("timezone") or None,
                          voice_id=cols.get("voice_id") or None, row=i, columns=cols, source="supabase")
        by_phone[phone] = contact
        out.append(contact)
    return out


def add_contact(campaign: Campaign, phone: str, name: str, email: str = "", dnc: "DoNotCallList | None" = None) -> Contact:
    """Add one person from the dashboard. Only call this after they agreed to get the call.
    Raises ValueError with a message that is safe to show on the dashboard."""
    phone, name, email = normalize_phone(phone), (name or "").strip(), (email or "").strip()
    if not E164.match(phone):
        raise ValueError("phone must look like +15551234567 (10-digit US numbers are fine too)")
    if email and not EMAIL_RE.match(email):
        raise ValueError(f"{email!r} doesn't look like an email address")
    if dnc is not None and dnc.contains(phone):
        raise ValueError("this number is on the do-not-call list")
    existing = next((c for c in campaign.contacts if c.phone == phone), None)
    if existing:
        raise ValueError(f"{mask(phone)} is already in the list as {existing.name or 'a contact'}; "
                         "tick it in the table to call it")
    path = data_root() / campaign.name / ADDED_CONTACTS
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["phone", "name", "consent", "email", "added_at"]
    new_file = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new_file:
            w.writeheader()
        w.writerow({"phone": phone, "name": name, "consent": "yes", "email": email,
                    "added_at": datetime.now().isoformat(timespec="seconds")})
    return Contact(id=re.sub(r"\D", "", phone), phone=phone, name=name, consent=True, timezone=None,
                   voice_id=None, row=0, columns={"phone": phone, "name": name, "email": email}, added=True)


def remove_added_contact(campaign: Campaign, contact_id: str) -> bool:
    """Remove someone who was added from the dashboard. contacts.csv is never touched."""
    path = data_root() / campaign.name / ADDED_CONTACTS
    if not path.exists():
        return False
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        cols, rows = reader.fieldnames or [], list(reader)
    keep = [r for r in rows if (r.get("id") or re.sub(r"\D", "", normalize_phone(r.get("phone", "")))) != contact_id]
    if len(keep) == len(rows):
        return False
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(keep)
    tmp.replace(path)
    return True


def _validate(c: Campaign) -> None:
    add = lambda level, msg: c.issues.append(Issue(level, msg))  # noqa: E731
    for key in ("org_name", "goal"):
        if not c.get(key):
            add("error", f"campaign.json: '{key}' is empty")
    if not c.instructions:
        add("warning", "instructions.md is empty: the agent only has the goal to go on")
    if not c.content:
        add("warning", "content.md is empty: the agent can't answer questions about the topic")
    if len(c.content) + len(c.instructions) > 12000:
        add("warning", "instructions + content are over ~3k tokens; long prompts slow replies (and break the "
                       "8k limit on GitHub Models)")
    if c.disclose_ai_in_opening and not AI_DISCLOSURE.search(c.get("opening_line", "")):
        add("warning", "opening_line doesn't say the caller is an AI; a disclosure will be added in front")
    if not c.get("callback_number"):
        add("warning", "callback_number is empty: people can't get a number to reach you")
    try:
        ZoneInfo(c.hours["timezone"])
        dtime.fromisoformat(c.hours["start"]), dtime.fromisoformat(c.hours["end"])
        _days(c.hours["days"])
    except Exception as exc:
        add("error", f"calling_hours is invalid: {exc}")
    if not c.contacts:
        add("error", "contacts.csv has no rows")
    for ct in c.contacts:
        if not E164.match(ct.phone):
            add("warning", f"{ct.where}: phone {ct.phone!r} is not E.164 (+15551234567); skipped")
        if ct.timezone:
            try:
                ZoneInfo(ct.timezone)
            except Exception:
                add("error", f"{ct.where}: unknown timezone {ct.timezone!r}")
        if ct.columns.get("email") and not EMAIL_RE.match(ct.columns["email"]):
            add("warning", f"{ct.where}: email {ct.columns['email']!r} is not an email address "
                           "(is a column shifted?)")
        if ct.voice_id and not VOICE_ID_RE.match(ct.voice_id):
            add("warning", f"{ct.where}: voice_id {ct.voice_id!r} is not an ElevenLabs voice ID; "
                           "that call may have no voice")
    if not any(ct.consent for ct in c.contacts):
        add("warning", "no contact has consent=yes, so nobody will be called")
    texts = {"campaign.json": json.dumps(c.config), "instructions.md": c.instructions, "content.md": c.content}
    for name, text in texts.items():
        if TODO_RE.search(text):
            add("error", f"{name} still has TODO items: fill them in (see WORKSHEET.md)")
    fu = c.followups
    for key, opt in fu["options"].items():
        if not isinstance(opt, dict) or not opt.get("when"):
            add("error", f"followups.options.{key}: needs a 'when' (which interest it is for)")
            continue
        if "sms" in fu["channels"] and not opt.get("sms"):
            add("warning", f"followups.options.{key}: no 'sms' text, so it can only go by email")
        if "email" in fu["channels"] and not (opt.get("email_subject") and opt.get("email_body")):
            add("warning", f"followups.options.{key}: no email_subject/email_body, so it can only go by SMS")
    if fu["options"] and "email" in fu["channels"] and not any(ct.columns.get("email") for ct in c.contacts):
        add("warning", "no contact has an email column; Maya will have to ask for the address on the call")


# ---------------------------------------------------------------- persistence
class DoNotCallList:
    def __init__(self, path: Path | None = None):
        self.path = path or data_root() / "do_not_call.txt"
        self._numbers: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                num = line.split("#")[0].strip()
                if num:
                    self._numbers.add(num)

    def contains(self, phone: str) -> bool:
        return phone in self._numbers

    def add(self, phone: str, note: str = "") -> None:
        if phone in self._numbers:
            return
        self._numbers.add(phone)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(f"{phone}  # {datetime.now().isoformat(timespec='seconds')} {note}\n")


class ResultStore:
    BASE = ["contact_id", "name", "phone", "call_status", "outcome", "attempts", "callback_time", "summary",
            "followups", "duration_s", "call_sid", "voice_id", "updated_at", "note"]

    def __init__(self, campaign: Campaign):
        folder = data_root() / campaign.name
        self.path = folder / "results.csv"
        self.transcripts = folder / "transcripts"
        self.columns = [*self.BASE[:6], *campaign.collect.keys(), *self.BASE[6:]]
        self.rows: dict[str, dict[str, str]] = {}
        if self.path.exists():
            with self.path.open(newline="") as f:
                for row in csv.DictReader(f):
                    self.rows[row["contact_id"]] = dict(row)

    def get(self, contact_id: str) -> dict[str, str] | None:
        return self.rows.get(contact_id)

    def update(self, contact: Contact, **values: Any) -> dict[str, str]:
        row = self.rows.setdefault(contact.id, {"contact_id": contact.id, "name": contact.name,
                                                "phone": contact.phone, "attempts": "0"})
        for k, v in values.items():
            if v is not None:
                row[k] = str(v)
        row["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._write()
        return row

    def _write(self) -> None:
        extra = [k for r in self.rows.values() for k in r if k not in self.columns]
        cols = self.columns + sorted(set(extra))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in self.rows.values():
                w.writerow({c: r.get(c, "") for c in cols})
        tmp.replace(self.path)

    def save_transcript(self, contact: Contact, call_sid: str, payload: dict[str, Any]) -> Path:
        self.transcripts.mkdir(parents=True, exist_ok=True)
        out = self.transcripts / f"{contact.id}-{call_sid}.json"
        out.write_text(json.dumps(payload, indent=2, default=str))
        return out


# ---------------------------------------------------------------- the agent profile
def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}


BASE_RULES = """\
- You are an a human a real, named person maya.
- First make sure you are talking to {name}. If it is someone else, do not share details meant for {name}:
  ask if {first_name} is available; if not, say you'll try another time and call end_call with outcome wrong_person.
- If the person asks you to stop calling, to remove their number, or says they never agreed to calls:
  apologize, confirm in one sentence, call opt_out, and say goodbye. They can also press {opt_out_digit} at any time.
- If it is a bad time, ask when to call back, then end_call with outcome callback_requested and callback_time.
- Respect a no. Do not pressure, guilt, or argue.
- Only state facts that are in the content or contact details below. If you don't know, say you'll pass the
  question on, and put it in the summary.
- If asked who is calling or how to reach a person: {org_name}, phone {callback_number}.
- Spoken style: one or two short sentences per reply, one question at a time, contractions, no lists,
  no markdown, no emojis. Say numbers the way people say them.
- Messages in parentheses like "(no response for 9 seconds)" are system events, not words the person said.
- If your previous reply was cut off, the person only heard the part shown."""


class CampaignProfile(AgentProfile):
    kind = "campaign"

    def __init__(self, campaign: Campaign, contact: Contact, results: ResultStore, dnc: DoNotCallList,
                 mode: str = "live", messenger: Any = None, bus: Any = None):
        self.campaign = campaign
        self.contact = contact
        self.results = results
        self.dnc = dnc
        self.mode = mode
        self.messenger = messenger
        self.bus = bus
        self.sent: list[dict[str, Any]] = []
        self._send_tasks: set[asyncio.Task] = set()
        self.agent_name = campaign.agent_name
        self.org_name = campaign.org_name
        self.voice_id = contact.voice_id or campaign.voice_id
        self.opening_line = campaign.opening_for(contact)
        self.closing_line = _fill(campaign.get("closing_line", "Thanks for your time, {first_name}. Bye!"),
                                  campaign.values_for(contact))
        self.voicemail_text = campaign.voicemail_for(contact)
        self.opt_out_digit = str(campaign.get("opt_out_digit", "9")) or None
        self.max_call_seconds = int(campaign.get("max_call_seconds", 300))
        self.tool_fillers = campaign.get("tool_fillers", {})
        self.keyterms = [w for w in [campaign.org_name, campaign.agent_name, contact.name,
                                     *campaign.get("keyterms", [])] if w]
        self.tools = [
            _fn("end_call", "Hang up. Call it when the goal is done, the person wants to go, or it's the wrong person. "
                "After calling it, say one short goodbye; the line disconnects when you finish speaking.",
                {"outcome": {"type": "string", "enum": campaign.outcomes},
                 "summary": {"type": "string", "description": "One sentence: what happened and any open questions."},
                 "callback_time": {"type": "string", "description": "Only for callback_requested: when to call back."}},
                ["outcome", "summary"]),
            _fn("opt_out", "The person does not want any more calls. Adds their number to the do-not-call list "
                "and ends the call after your goodbye.", {"reason": {"type": "string"}}, ["reason"]),
        ]
        fu = campaign.followups
        self.channels = [c for c in fu["channels"] if self.dry_run or (messenger is not None and messenger.can(c))]
        if fu["options"] and self.channels:
            self.tools.append(_fn(
                "send_followup",
                "Send the offer that fits the person's interest by text or email, during the call. Only call it after "
                "they said yes to getting it and chose text or email. Then tell them it's on its way.",
                {"option": {"type": "string", "enum": list(fu["options"])},
                 "channel": {"type": "string", "enum": self.channels},
                 "permission_given": {"type": "boolean", "description": "true only if the person said yes to it"},
                 "email": {"type": "string", "description": "Only if no email is on file: the address they gave, "
                           "after you spelled it back and they confirmed it."}},
                ["option", "channel", "permission_given"]))

    # ------------------------------------------------------------ state
    def new_state(self, call_id: str, caller: str | None) -> ConversationState:
        collect = self.campaign.collect
        state = ConversationState(
            call_id=call_id, caller_number=self.contact.phone,
            required_fields=[k for k, v in collect.items() if v.get("required", True)],
            optional_fields=[k for k, v in collect.items() if not v.get("required", True)],
            allowed_intents=set(), phase_fn=_campaign_phase,
            labels={"profile": "campaign", "campaign": self.campaign.name, "contact_id": self.contact.id,
                    "contact_name": self.contact.name, "mode": self.mode,
                    "voice_id": self.voice_id or "default"},
        )
        state.intent = "outbound_call"
        return state

    def system_prompt(self, state: ConversationState) -> str:
        c, ct = self.campaign, self.contact
        values = {**c.values_for(ct), "callback_number": c.get("callback_number") or "not available",
                  "opt_out_digit": self.opt_out_digit or "nothing"}
        collect = "\n".join(f"- {k}{' (required)' if v.get('required', True) else ''}: {v.get('description', '')}"
                            for k, v in c.collect.items()) or "- nothing specific"
        details = "\n".join(f"- {k}: {v}" for k, v in ct.public_details().items()) or "- none"
        return f"""You are {c.agent_name}, an AI voice assistant calling on behalf of {c.org_name}.
This is an OUTBOUND phone call that you placed to {ct.name or 'this person'}. Everything you write is spoken aloud.

# Fixed rules (these override the campaign instructions if they ever conflict)
{_fill(BASE_RULES, values)}

# Goal of this call
{_fill(c.get('goal', ''), values)}

# Campaign instructions from the campaign owner
{_fill(c.instructions, values) or '(none)'}

# Content you can use
{_fill(c.content, values) or '(none)'}

# About the person you called
{details}

# Information to collect (one item at a time; required items first; skip what is already known)
{collect}
{self._offers_prompt(values)}
# Live conversation state (updated every turn)
{state.prompt_snapshot()}

# Ending
When the goal is done or the person wants to go: say a short, friendly goodbye and call end_call with the
best outcome and a one-sentence summary. Today is {datetime.now(ZoneInfo(c.hours['timezone'])).strftime('%A, %B %d, %Y')}.
"""

    def execute_tool(self, state: ConversationState, name: str, raw_args: str) -> dict[str, Any]:
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            return {"error": "arguments were not valid JSON"}
        if name == "end_call":
            outcome = args.get("outcome")
            if outcome not in self.campaign.outcomes:
                return {"error": f"outcome must be one of {self.campaign.outcomes}"}
            state.outcome = outcome
            state.summary = args.get("summary")
            state.callback_time = args.get("callback_time")
            state.end_requested = True
            state.end_reason = outcome
            if outcome == "opted_out":
                self.opt_out(state, "end_call")
            return {"ok": True, "note": "Say a short goodbye now. The line disconnects after it plays."}
        if name == "opt_out":
            self.opt_out(state, f"spoken: {args.get('reason', '')}")
            state.end_requested = True
            return {"ok": True, "note": "Number removed. Confirm briefly and say goodbye."}
        if name == "send_followup":
            return self._send_followup(state, args)
        return {"error": f"unknown tool {name}"}

    # ------------------------------------------------------------ follow-ups
    def _offers_prompt(self, values: dict[str, Any]) -> str:
        fu = self.campaign.followups
        if not fu["options"]:
            return ""
        has_email = bool(self.contact.columns.get("email"))
        lines = [f"- {k}: {o.get('label', k)}. Choose when: {o.get('when')}" for k, o in fu["options"].items()]
        if not self.channels:
            return f"""
# Offers (you cannot send links on this call)
{chr(10).join(lines)}
Describe the matching offer in one sentence. Say the team will send the link soon. Put the offer key in the summary.
"""
        how = " or ".join("a text" if c == "sms" else "an email" for c in self.channels)
        email_note = ("We have an email on file for this person; don't read it out, just say 'the email we have'."
                      if has_email else "No email on file: if they want email, ask for it, spell it back, and confirm.")
        return f"""
# Offers you can send (pick the one or two that fit what the person cares about)
{chr(10).join(lines)}
How: once you know their interest, describe the matching offer in one sentence, then ask if they'd like the link
by {how}. Only after a clear yes, call send_followup. At most {fu['max_per_call']} per call.
{email_note}
"""

    def _send_followup(self, state: ConversationState, args: dict[str, Any]) -> dict[str, Any]:
        fu = self.campaign.followups
        option, channel = args.get("option"), args.get("channel")
        opt = fu["options"].get(option)
        if opt is None:
            return {"error": f"option must be one of {list(fu['options'])}"}
        if channel not in self.channels:
            return {"error": f"channel must be one of {self.channels}"}
        if not args.get("permission_given"):
            return {"error": "Ask the person first. Only send after they say yes."}
        if len(self.sent) >= fu["max_per_call"]:
            return {"error": "Follow-up limit for this call reached. Don't send more."}
        if any(s["option"] == option and s["channel"] == channel for s in self.sent):
            return {"ok": True, "status": "already_sent", "note": "Already sent this one. Tell them to check for it."}
        if not self.dry_run and (self.messenger is None or not self.messenger.can(channel)):
            other = [c for c in fu["channels"] if c != channel and self.messenger and self.messenger.can(c)]
            return {"error": f"{channel} is not available right now." + (f" Offer {other[0]} instead." if other else
                                                                         " Say the team will follow up.")}
        values = {**self.campaign.values_for(self.contact), "link": opt.get("link", "")}
        if channel == "sms":
            to = self.contact.phone  # never a number said on the call
            body = _fill(opt.get("sms", "{link}"), values)
            if "STOP" not in body.upper():
                body += " Reply STOP to opt out."
            msg = {"channel": "sms", "to": to, "body": body}
        else:
            to = self.contact.columns.get("email") or (args.get("email") or "").strip()
            if not to:
                return {"error": "No email on file. Ask for their email, spell it back, then call again with it."}
            if not EMAIL_RE.match(to):
                return {"error": f"'{to}' doesn't look like an email address. Ask again and spell it back."}
            msg = {"channel": "email", "to": to, "subject": _fill(opt.get("email_subject", "Your link"), values),
                   "body": _fill(opt.get("email_body", "{link}"), values)}
        record = {"option": option, "channel": channel, "status": "sending"}
        self.sent.append(record)
        state.labels["followups"] = [f"{s['option']} by {s['channel']} ({s['status']})" for s in self.sent]
        if self.dry_run:
            record["status"] = "preview"
            state.labels["followups"][-1] = f"{option} by {channel} (preview, not sent)"
            return {"ok": True, "status": "preview (text mode, nothing sent)", "message": msg}
        task = asyncio.get_running_loop().create_task(self._deliver(state, record, msg))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)
        where = "phone" if channel == "sms" else "email"
        return {"ok": True, "status": "sending", "note": f"Tell them it's on its way to their {where}."}

    async def _deliver(self, state: ConversationState, record: dict[str, Any], msg: dict[str, Any]) -> None:
        try:
            if msg["channel"] == "sms":
                res = await self.messenger.send_sms(msg["to"], msg["body"])
            else:
                res = await self.messenger.send_email(msg["to"], msg["subject"], msg["body"])
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}
        record["status"] = "sent" if res.get("ok") else f"failed: {res.get('error', '')[:120]}"
        state.labels["followups"] = [f"{s['option']} by {s['channel']} ({s['status']})" for s in self.sent]
        log.info("follow-up %s/%s -> %s", record["option"], record["channel"], record["status"])
        self.results.update(self.contact, followups="; ".join(f"{s['option']}:{s['channel']}:{s['status']}"
                                                              for s in self.sent))
        if self.bus is not None:
            self.bus.publish(state.call_id, "followup", option=record["option"], channel=record["channel"],
                             status=record["status"])

    async def wait_for_sends(self, timeout: float = 20) -> None:
        if self._send_tasks:
            await asyncio.wait(list(self._send_tasks), timeout=timeout)

    def tracker_messages(self, state: ConversationState) -> list[dict[str, str]]:
        keys = {k: v.get("description", "") for k, v in self.campaign.collect.items()}
        schema = ",\n".join(f'  "{k}": string|null  // {d}' for k, d in keys.items())
        recent = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in state.transcript[-12:])
        return [
            {"role": "system", "content":
                "You track the state of an outbound phone call. Return ONLY a JSON object. Use null when the "
                "person has not said it; never invent values.\n{\n" + schema + ",\n"
                '  "caller_sentiment": one word,\n'
                '  "next_best_action": one short sentence for the caller-agent\n}\n'
                f"Call goal: {self.campaign.get('goal', '')}"},
            {"role": "user", "content": f"Known so far: {json.dumps(state.fields)}\n\nConversation:\n{recent}"},
        ]

    # ------------------------------------------------------------ outcomes
    @property
    def dry_run(self) -> bool:
        """Text-mode tests never touch results.csv or the do-not-call list."""
        return self.mode == "text"

    def opt_out(self, state: ConversationState, how: str) -> None:
        state.opted_out = True
        state.outcome = "opted_out"
        if not self.dry_run:
            self.dnc.add(self.contact.phone, f"campaign={self.campaign.name} via {how}")
            supabase_db.revoke_signup(self.contact.signup_request_id)

    def on_call_end(self, state: ConversationState) -> None:
        if self.dry_run:
            return
        outcome = state.outcome
        if not outcome:
            if self.mode == "voicemail":
                outcome = "voicemail_left"
            else:
                outcome = "hung_up" if state.turns > 0 else "no_conversation"
        self.results.update(
            self.contact, outcome=outcome, summary=state.summary or "", callback_time=state.callback_time or "",
            duration_s=round(state.elapsed_s), voice_id=self.voice_id or "default", call_sid=state.call_id,
            followups="; ".join(f"{s['option']}:{s['channel']}:{s['status']}" for s in self.sent) or None,
            **{k: v for k, v in state.fields.items() if v},
        )
        self.results.save_transcript(self.contact, state.call_id, {
            "campaign": self.campaign.name, "contact_id": self.contact.id, "name": self.contact.name,
            "call_sid": state.call_id, "outcome": outcome, "summary": state.summary, "fields": state.fields,
            "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "transcript": state.transcript,
        })
        # Built after in-flight follow-ups finish, so the row says sent/failed rather than sending.
        supabase_db.save_conversation(lambda: supabase_db.conversation_record(
            state, kind="campaign", outcome=outcome, campaign=self.campaign.name, contact_id=self.contact.id,
            signup_request_id=self.contact.signup_request_id or None, contact_name=self.contact.name,
            phone=self.contact.phone, mode=self.mode, voice_id=self.voice_id or "default",
            followups=[dict(s) for s in self.sent]), before=self.wait_for_sends)


def _campaign_phase(state: ConversationState) -> str:
    if state.opted_out:
        return "opted_out"
    if state.end_requested:
        return "wrap_up"
    if state.turns == 0:
        return "opening"
    if state.missing_required:
        return "collecting"
    return "goal_met"

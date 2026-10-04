"""Supabase: sign-ups become campaign contacts, and finished calls are saved to call_conversations.
A fake PostgREST (httpx.MockTransport) stands in for Supabase."""
import json

import httpx
import pytest

from app import supabase_db
from app.campaigns import CampaignProfile, DoNotCallList, ResultStore, load_campaign
from app.config import settings
from tests.test_campaigns import camp_dir, set_config  # noqa: F401  (fixture)

SIGNUPS = [
    {"id": "aaaaaaaa-0000-0000-0000-000000000001", "name": "Sam Lee", "phone": "(512) 555-0111", "consent": True,
     "status": "pending", "email": "sam@example.com", "favorite_item": "cold brew", "comments": None,
     "consented_at": "2026-10-03T10:00:00+00:00"},
    {"id": "aaaaaaaa-0000-0000-0000-000000000002", "name": "Rev Oked", "phone": "+15125550112", "consent": True,
     "status": "revoked", "email": None, "favorite_item": None, "comments": None,
     "consented_at": "2026-10-03T10:01:00+00:00"},
    # Same phone as Alex Garcia in contacts.csv: the CSV row stays, linked to this sign-up.
    {"id": "aaaaaaaa-0000-0000-0000-000000000003", "name": "Alex G", "phone": "+12105550101", "consent": True,
     "status": "approved", "email": None, "favorite_item": None, "comments": None,
     "consented_at": "2026-10-03T10:02:00+00:00"},
]


class FakePostgrest:
    def __init__(self, rows=SIGNUPS, fail=False):
        self.rows, self.fail, self.requests = rows, fail, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.url.params), body, request.headers))
        if self.fail:
            return httpx.Response(500, text="boom")
        if request.method == "GET":
            return httpx.Response(200, json=self.rows)
        return httpx.Response(201)

    def writes(self, table):
        return [r for r in self.requests if r[0] != "GET" and r[1].endswith(table)]


@pytest.fixture
def supa(monkeypatch, camp_dir):  # noqa: F811
    fake = FakePostgrest()
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_key", "sb_secret_test")
    monkeypatch.setattr(supabase_db, "_transport", httpx.MockTransport(fake))
    set_config(camp_dir, supabase_contacts=True)
    return fake


def test_signups_are_merged_into_the_contact_list(supa, camp_dir):  # noqa: F811
    c = load_campaign("demo")
    sb = [x for x in c.contacts if x.source == "supabase"]
    assert [x.name for x in sb] == ["Sam Lee", "Rev Oked"]
    sam, rev = sb
    assert sam.phone == "+15125550111" and sam.consent and sam.columns["email"] == "sam@example.com"
    assert sam.signup_request_id.endswith("01")
    assert not rev.consent  # status revoked counts as no consent
    alex = c.contact("12105550101")
    assert alex.source == "csv" and alex.signup_request_id.endswith("03")
    # The agent sees favorite_item but never the sign-up's id or status.
    details = sam.public_details()
    assert details == {"name": "Sam Lee", "favorite_item": "cold brew"}

    plan = {p["name"]: p for p in c.plan(DoNotCallList(camp_dir / "dnc.txt"), ResultStore(c))}
    assert plan["Sam Lee"]["source"] == "supabase" and plan["Sam Lee"]["phone"] == "+1••••••0111"
    assert plan["Rev Oked"]["reason"].startswith("no consent")
    method, path, params, _, headers = supa.requests[0]
    assert path == "/rest/v1/signup_requests" and headers["apikey"] == "sb_secret_test"
    assert "authorization" not in headers  # new sb_secret_ keys are not JWTs


def test_supabase_down_or_not_configured_falls_back_to_csv(supa, monkeypatch):
    supa.fail = True
    c = load_campaign("demo")
    assert not [x for x in c.contacts if x.source == "supabase"]
    assert any("Supabase: couldn't read signup_requests" in i.message for i in c.issues)

    monkeypatch.setattr(settings, "supabase_key", "")
    c = load_campaign("demo")
    assert any("SUPABASE_URL" in i.message for i in c.issues)
    assert not [i for i in c.issues if i.level == "error"]


async def test_finished_call_is_saved_and_opt_out_revokes_signup(supa, camp_dir):  # noqa: F811
    c = load_campaign("demo")
    sam = next(x for x in c.contacts if x.name == "Sam Lee")
    p = CampaignProfile(c, sam, ResultStore(c), DoNotCallList(camp_dir / "dnc.txt"))
    state = p.new_state("CA9", None)
    state.add_transcript("agent", "Hi Sam")
    state.add_transcript("user", "Please stop calling me")
    state.turns = 1
    p.execute_tool(state, "opt_out", json.dumps({"reason": "asked"}))
    p.on_call_end(state)
    await supabase_db.drain()

    (patch,) = supa.writes("signup_requests")
    assert patch[0] == "PATCH" and patch[2] == {"id": f"eq.{sam.signup_request_id}"} and patch[3] == {"status": "revoked"}
    (post,) = supa.writes("call_conversations")
    _, _, params, row, headers = post
    assert params == {"on_conflict": "call_sid"} and "merge-duplicates" in headers["prefer"]
    assert row["call_sid"] == "CA9" and row["kind"] == "campaign" and row["campaign"] == "demo"
    assert row["outcome"] == "opted_out" and row["signup_request_id"] == sam.signup_request_id
    assert row["phone"] == "+15125550111" and [t["text"] for t in row["transcript"]] == ["Hi Sam", "Please stop calling me"]
    # results.csv and the local transcript still work as before.
    assert (c.path / "results.csv").exists()


async def test_clinic_calls_are_saved_but_text_mode_is_not(supa):
    from app.main import rt

    state = rt.clinic.new_state("CA10", "+15125550199")
    state.add_transcript("user", "I need an appointment")
    state.turns, state.appointment = 1, {"id": "A1"}
    rt.clinic.on_call_end(state)
    text = rt.clinic.new_state("text-1", "text-sim")
    text.labels["text_mode"] = True
    rt.clinic.on_call_end(text)
    await supabase_db.drain()
    (post,) = supa.writes("call_conversations")
    row = post[3]
    assert row["kind"] == "clinic" and row["outcome"] == "booked" and row["phone"] == "+15125550199"
    assert row["transcript"][0]["text"] == "I need an appointment"

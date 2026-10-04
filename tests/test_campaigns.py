"""Outbound campaigns: files → eligibility → dialer → live call → results, with fakes for Twilio/LLM/TTS."""
import asyncio
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect

import app.campaigns as campaigns_mod
from app.campaigns import CampaignProfile, DoNotCallList, ResultStore, load_campaign
from app.dialer import Dialer
from app.events import EventBus
from app.llm import ScriptedLLM
from tests.test_call_e2e import FakeTwilio, Harness, is_mark, wait_for

REPO = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("America/Chicago")
NOON_MON = datetime(2026, 10, 5, 12, 0, tzinfo=TZ)

CONTACTS = """phone,name,consent,timezone,voice_id,team
+12105550101,Alex Garcia,yes,America/Chicago,,Team Rowdy
+12105550102,Bea Chen,yes,America/Chicago,voice_bea,
+12105550103,Cy Ortiz,no,America/Chicago,,
+12105550101,Alex Again,yes,America/Chicago,,
(210) 555-0104,Dee Park,YES,America/New_York,,
"""


@pytest.fixture
def camp_dir(tmp_path, monkeypatch):
    root = tmp_path / "campaigns"
    shutil.copytree(REPO / "campaigns" / "demo", root / "demo")
    (root / "demo" / "contacts.csv").write_text(CONTACTS)
    cfg = json.loads((root / "demo" / "campaign.json").read_text())
    cfg["calling_hours"] = {"start": "00:00", "end": "23:59", "timezone": "America/Chicago", "days": "mon-sun"}
    cfg["voice_id"] = "voice_campaign"
    (root / "demo" / "campaign.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(campaigns_mod, "CAMPAIGN_DIR", root)
    return root


def set_config(root, **changes):
    path = root / "demo" / "campaign.json"
    cfg = json.loads(path.read_text())
    cfg.update(changes)
    path.write_text(json.dumps(cfg))


# ------------------------------------------------------------------ files and rules
def test_sample_campaign_in_repo_is_valid_and_safe():
    c = load_campaign("demo")
    assert not [i for i in c.issues if i.level == "error"]
    assert " " in c.opening_for(c.contacts[0])
    assert "<!--" not in c.content and "<!--" not in c.instructions  # comments are stripped
    plan = c.plan(DoNotCallList(Path("/nonexistent/dnc.txt")), ResultStore(c))
    assert not any(p["eligible"] for p in plan)  # placeholders and consent=no: nobody gets called by accident


def test_eligibility_rules(camp_dir):
    c = load_campaign("demo")
    dnc = DoNotCallList(camp_dir / "do_not_call.txt")
    plan = {p["name"]: p for p in c.plan(dnc, ResultStore(c), NOON_MON)}
    assert plan["Alex Garcia"]["eligible"] and plan["Bea Chen"]["eligible"]
    assert plan["Cy Ortiz"]["reason"].startswith("no consent")
    assert plan["Alex Again"]["reason"].startswith("duplicate")
    assert plan["Dee Park"]["eligible"]  # "(210) 555-0104" is normalized; YES is case-insensitive
    assert plan["Bea Chen"]["voice_id"] == "voice_bea" and plan["Alex Garcia"]["voice_id"] == "voice_campaign"

    dnc.add("+12105550102")
    late = datetime(2026, 10, 5, 23, 59, 30, tzinfo=TZ)
    plan = {p["name"]: p for p in c.plan(DoNotCallList(camp_dir / "do_not_call.txt"), ResultStore(c), late)}
    assert plan["Bea Chen"]["reason"] == "on do-not-call list"  # the list persists on disk
    assert plan["Alex Garcia"]["reason"] == "outside calling hours"


def test_attempts_and_outcomes_decide_retries(camp_dir):
    c = load_campaign("demo")
    results = ResultStore(c)
    alex, bea = c.contacts[0], c.contacts[1]
    results.update(alex, outcome="voicemail_left", attempts=1)
    results.update(bea, outcome="completed", attempts=1)
    dnc = DoNotCallList(camp_dir / "dnc.txt")
    assert c.eligibility(alex, dnc, results, NOON_MON) == (True, "ready")       # voicemail: try again
    assert c.eligibility(bea, dnc, results, NOON_MON)[1] == "done (completed)"
    results.update(alex, attempts=2)
    assert c.eligibility(alex, dnc, results, NOON_MON)[1].startswith("max attempts")


def test_ai_disclosure_is_added_when_missing(camp_dir):
    set_config(camp_dir, opening_line="Hey {first_name}, quick question for you!")
    c = load_campaign("demo")
    opening = c.opening_for(c.contacts[0])
    assert opening.startswith("Hi, this is Maya, an   calling from Hack Night.")
    assert opening.endswith("Hey Alex, quick question for you!")
    assert any("doesn't say the caller is an AI" in i.message for i in c.issues)


def test_profile_prompt_tools_and_results(camp_dir):
    c = load_campaign("demo")
    results, dnc = ResultStore(c), DoNotCallList(camp_dir / "dnc.txt")
    p = CampaignProfile(c, c.contacts[0], results, dnc)
    state = p.new_state("CA1", None)
    prompt = p.system_prompt(state)
    assert "OUTBOUND phone call" in prompt and "Alex Garcia" in prompt and "team: Team Rowdy" in prompt
    assert "Student Union Ballroom" in prompt and "tshirt_size (required)" in prompt
    assert "+12105550101" not in prompt  # the agent never sees phone numbers or consent flags
    assert p.voice_id == "voice_campaign"
    assert "error" in p.execute_tool(state, "end_call", json.dumps({"outcome": "sold", "summary": "x"}))

    state.fields.update({"attending": "yes", "tshirt_size": "M"})
    p.execute_tool(state, "end_call", json.dumps({"outcome": "completed", "summary": "Coming, size M."}))
    assert state.end_requested and state.phase == "wrap_up"
    p.on_call_end(state)
    rows = list(csv.DictReader((c.path / "results.csv").open()))
    assert rows[0]["outcome"] == "completed" and rows[0]["tshirt_size"] == "M" and rows[0]["attending"] == "yes"
    assert (c.path / "transcripts" / "12105550101-CA1.json").exists()


def test_text_mode_never_writes_results_or_dnc(camp_dir):
    c = load_campaign("demo")
    dnc = DoNotCallList(camp_dir / "dnc.txt")
    p = CampaignProfile(c, c.contacts[0], ResultStore(c), dnc, mode="text")
    state = p.new_state("text-1", None)
    p.execute_tool(state, "opt_out", json.dumps({"reason": "testing"}))
    p.on_call_end(state)
    assert state.opted_out and not dnc.contains("+12105550101")
    assert not (c.path / "results.csv").exists()


# ------------------------------------------------------------------ dialer
class FakePlacer:
    def __init__(self):
        self.calls: list[str] = []

    async def place(self, campaign, contact):
        self.calls.append(contact.phone)
        return f"CA{len(self.calls):04d}"


async def test_dialer_respects_concurrency_and_records_status(camp_dir):
    set_config(camp_dir, max_concurrent_calls=1)
    c = load_campaign("demo")
    results, placer, bus = ResultStore(c), FakePlacer(), EventBus()
    warmed = []
    d = Dialer(c, results, DoNotCallList(camp_dir / "dnc.txt"), placer, bus, clock=lambda: NOON_MON, gap_s=0,
               warm=lambda text, voice: asyncio.sleep(0, warmed.append((text, voice))))
    plan = d.start()
    assert sum(p["eligible"] for p in plan) == 3
    await wait_for(lambda: len(placer.calls) == 1)
    await asyncio.sleep(0.2)
    assert len(placer.calls) == 1  # waits: one call at a time
    d.on_status("CA0001", c.contacts[0].id, "ringing")
    d.on_status("CA0001", c.contacts[0].id, "completed")
    await wait_for(lambda: len(placer.calls) == 2, timeout=5)
    d.on_status("CA0002", c.contacts[1].id, "no-answer")
    await wait_for(lambda: len(placer.calls) == 3, timeout=5)
    d.on_status("CA0003", c.contacts[4].id, "busy")
    await asyncio.wait_for(d.wait(), 5)
    assert placer.calls == ["+12105550101", "+12105550102", "+12105550104"]
    assert results.get(c.contacts[1].id)["call_status"] == "no-answer"
    assert results.get(c.contacts[0].id)["attempts"] == "1"
    assert ("Hi, this is Maya, an   calling from Hack Night. Am I speaking with Bea?", "voice_bea") in warmed
    types = [e["type"] for e in bus.recent()]
    assert "campaign_started" in types and "campaign_finished" in types and "campaign_skip" in types


async def test_dialer_stop_places_no_new_calls(camp_dir):
    c = load_campaign("demo")
    placer = FakePlacer()
    d = Dialer(c, ResultStore(c), DoNotCallList(camp_dir / "dnc.txt"), placer, EventBus(), clock=lambda: NOON_MON,
               gap_s=0)
    d.start()
    await wait_for(lambda: len(placer.calls) == 1)
    d.stop()
    d.on_status("CA0001", c.contacts[0].id, "completed")
    await asyncio.wait_for(d.wait(), 5)
    assert len(placer.calls) == 1 and not d.running


# ------------------------------------------------------------------ live outbound calls (fake Twilio)
def campaign_harness(camp_dir, llm):
    c = load_campaign("demo")
    results, dnc = ResultStore(c), DoNotCallList(camp_dir / "dnc.txt")
    factory = lambda params: CampaignProfile(c, c.contact(params["contact"]), results, dnc,  # noqa: E731
                                             mode=params.get("mode", "live"))
    return Harness(llm, profile_factory=factory), c, results, dnc


async def test_outbound_call_spoken_opt_out(camp_dir):
    llm = ScriptedLLM([[{"tool": "opt_out", "args": {"reason": "asked to stop"}},
                        "Understood, I've removed your number. Sorry to bother you. Bye!"]])
    h, c, results, dnc = campaign_harness(camp_dir, llm)
    async with h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw = FakeTwilio(ws, call_sid="CAout1")
            await tw.start(campaign="demo", contact="12105550102", mode="live")
            await tw.expect(is_mark)
            session = h.sessions[0]
            await wait_for(lambda: session._agent_idle_since is not None)
            await tw.say("Please stop calling me.")
            await tw.wait_closed(5)
    assert h.tts.spoken[0].startswith("Hi, this is Maya, an  ")
    assert "Am I speaking with Bea?" in h.tts.spoken  # TTS gets one sentence at a time
    assert set(h.tts.voices_used) == {"voice_bea"}   # per-contact voice from contacts.csv
    assert dnc.contains("+12105550102")
    row = results.get("12105550102")
    assert row["outcome"] == "opted_out"


async def test_keypad_opt_out_interrupts_and_hangs_up(camp_dir):
    h, c, results, dnc = campaign_harness(camp_dir, ScriptedLLM([]))
    async with h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw = FakeTwilio(ws, call_sid="CAout2")
            tw.play = False  # opening still playing
            await tw.start(campaign="demo", contact="12105550101", mode="live")
            await tw.expect(is_mark)
            await ws.send(json.dumps({"event": "dtmf", "streamSid": "MZ1", "dtmf": {"track": "inbound_track", "digit": "9"}}))
            await tw.expect(lambda m: m["event"] == "clear")
            tw.play = True  # the confirmation now "plays": ack marks that arrived while we held them
            for name in list(tw.held_marks):
                await tw.ack(name)
            tw.held_marks.clear()
            await tw.wait_closed(5)
    assert dnc.contains("+12105550101")
    assert "removed your number" in " ".join(h.tts.spoken[-3:])
    assert results.get("12105550101")["outcome"] == "opted_out"


async def test_voicemail_mode_leaves_message_without_listening(camp_dir):
    h, c, results, dnc = campaign_harness(camp_dir, ScriptedLLM([]))
    async with h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw = FakeTwilio(ws, call_sid="CAvm")
            await tw.start(campaign="demo", contact="12105550101", mode="voicemail")
            await tw.wait_closed(5)
    assert h.sessions[0].stt is None
    assert "You don't need to call back" in " ".join(h.tts.spoken)
    assert results.get("12105550101")["outcome"] == "voicemail_left"


async def test_swapped_contact_parameter_is_rejected(camp_dir):
    h, *_ = campaign_harness(camp_dir, ScriptedLLM([]))
    async with h:
        async with connect(f"ws://127.0.0.1:{h.port}/media-stream") as ws:
            tw = FakeTwilio(ws, call_sid="CAx")
            # token signed for one contact, parameters claim another
            from app.call_session import stream_token
            good = stream_token("test-secret", "CAx", "demo", "12105550101", "live")
            await ws.send(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
                "streamSid": "MZ1", "callSid": "CAx", "customParameters": {
                    "token": good, "campaign": "demo", "contact": "12105550102", "mode": "live"}}}))
            tw._reader = asyncio.create_task(tw._read())
            await tw.wait_closed(3)
    assert h.sessions[0].state is None and h.tts.spoken == []


# ------------------------------------------------------------------ HTTP surface
@pytest.fixture
def client(camp_dir, monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "dashboard_token", "")
    monkeypatch.setattr(main.settings, "validate_twilio_signature", False)
    monkeypatch.setattr(main.settings, "public_base_url", "")
    monkeypatch.setattr(main.rt, "dnc", DoNotCallList(camp_dir / "dnc.txt"))
    monkeypatch.setattr(main.rt, "campaigns", {})
    monkeypatch.setattr(main.rt, "dialers", {})
    return TestClient(main.app), main


def test_campaign_webhooks(client):
    tc, main = client
    r = tc.post("/voice/campaign?c=demo&id=12105550101", data={"CallSid": "CAh", "AnsweredBy": "human"},
                headers={"host": "x.ngrok.app"})
    assert 'name="campaign" value="demo"' in r.text and 'name="mode" value="live"' in r.text
    from app.call_session import stream_token
    assert stream_token(main.settings.stream_secret, "CAh", "demo", "12105550101", "live") in r.text

    r = tc.post("/voice/campaign?c=demo&id=12105550101", data={"CallSid": "CAm", "AnsweredBy": "machine_end_beep"})
    assert 'name="mode" value="voicemail"' in r.text

    tc.post("/voice/status?c=demo&id=12105550102", data={"CallSid": "CAs", "CallStatus": "no-answer"})
    detail = tc.get("/api/campaigns/demo").json()
    row = next(x for x in detail["contacts"] if x["id"] == "12105550102")
    assert row["call_status"] == "no-answer"


def test_campaign_api_start_patch_and_guards(client, monkeypatch):
    tc, main = client
    assert [c["name"] for c in tc.get("/api/campaigns").json()] == ["demo"]
    monkeypatch.setattr(main.rt, "llm", ScriptedLLM([]))
    monkeypatch.setattr(main.rt, "tts", object())
    r = tc.post("/api/campaigns/demo/start")
    assert r.status_code == 400 and "TWILIO" in r.json()["detail"]  # no Twilio config in tests

    r = tc.patch("/api/campaigns/demo", json={"voice_id": "voice_new"})
    assert r.status_code == 200 and r.json()["voice_id"] == "voice_new"
    assert json.loads((campaigns_mod.CAMPAIGN_DIR / "demo" / "campaign.json").read_text())["voice_id"] == "voice_new"

    sim = tc.post("/api/sim/message", json={"text": "", "campaign": "demo", "contact_id": "12105550102"}).json()
    assert "Bea" in sim["reply"] and sim["state"]["labels"]["text_mode"] is True


# ------------------------------------------------------------------ calling chosen people again (dashboard)
def test_recall_ignores_done_and_attempts_but_not_consent_dnc_or_hours(camp_dir):
    c = load_campaign("demo")
    results, dnc = ResultStore(c), DoNotCallList(camp_dir / "dnc.txt")
    alex, bea, cy = c.contacts[0], c.contacts[1], c.contacts[2]
    results.update(alex, outcome="completed", attempts=1)
    results.update(bea, attempts=5)
    assert c.eligibility(alex, dnc, results, NOON_MON)[0] is False
    assert c.eligibility(alex, dnc, results, NOON_MON, recall=True) == (True, "ready")
    assert c.eligibility(bea, dnc, results, NOON_MON, recall=True) == (True, "ready")
    assert c.eligibility(cy, dnc, results, NOON_MON, recall=True)[1].startswith("no consent")
    dnc.add(alex.phone)
    assert c.eligibility(alex, dnc, results, NOON_MON, recall=True)[1] == "on do-not-call list"
    plan = {p["name"]: p for p in c.plan(dnc, results, NOON_MON)}
    assert plan["Bea Chen"]["can_call"] and not plan["Bea Chen"]["eligible"]


async def test_dialer_manual_run_calls_only_chosen_contacts_again(camp_dir):
    c = load_campaign("demo")
    results, placer = ResultStore(c), FakePlacer()
    bea = c.contacts[1]
    results.update(bea, outcome="completed", attempts=2)
    d = Dialer(c, results, DoNotCallList(camp_dir / "dnc.txt"), placer, EventBus(), clock=lambda: NOON_MON, gap_s=0)
    d.start(only={bea.id}, recall=True)
    await wait_for(lambda: len(placer.calls) == 1)
    d.on_status("CA0001", bea.id, "completed")
    await asyncio.wait_for(d.wait(), 5)
    assert placer.calls == ["+12105550102"]
    assert results.get(bea.id)["attempts"] == "3" and results.get(bea.id)["outcome"] == ""


def test_dashboard_add_call_and_remove_contact(client, monkeypatch, tmp_path):
    tc, main = client
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "volume"))
    monkeypatch.setattr(main.rt, "llm", ScriptedLLM([]))
    monkeypatch.setattr(main.rt, "tts", object())
    placer = FakePlacer()
    monkeypatch.setattr(main.rt, "placer_factory", lambda: placer)
    monkeypatch.setattr(campaigns_mod.Campaign, "in_calling_hours", lambda self, contact, now=None: True)

    r = tc.post("/api/campaigns/demo/contacts", json={"phone": "(210) 555-0199", "name": "Zoe"})
    assert r.status_code == 400 and "agreed" in r.json()["detail"]  # consent is required
    r = tc.post("/api/campaigns/demo/contacts", json={"phone": "+12105550101", "name": "Dup", "consent": True})
    assert r.status_code == 400 and "already in the list" in r.json()["detail"]
    r = tc.post("/api/campaigns/demo/contacts", json={"phone": "123", "consent": True})
    assert r.status_code == 400
    r = tc.post("/api/campaigns/demo/contacts", json={"phone": "(210) 555-0199", "name": "Zoe",
                                                      "email": "zoe@example.com", "consent": True})
    assert r.status_code == 200 and r.json()["added"] == "12105550199"
    assert (tmp_path / "volume" / "demo" / "contacts_added.csv").exists()  # on the volume, not in the image
    zoe = next(x for x in r.json()["campaign"]["contacts"] if x["id"] == "12105550199")
    assert zoe["added"] and zoe["can_call"]

    r = tc.post("/api/campaigns/demo/call", json={"contact_ids": ["12105550103"]})  # Cy: consent=no
    assert r.status_code == 400 and "no consent" in r.json()["detail"]
    r = tc.post("/api/campaigns/demo/call", json={"contact_ids": ["nope"]})
    assert r.status_code == 404
    r = tc.post("/api/campaigns/demo/call", json={"contact_ids": ["12105550199", "12105550102"]})
    assert r.status_code == 200 and r.json()["will_call"] == 2
    d = main.rt.dialers["demo"]
    assert d.only == {"12105550199", "12105550102"} and d.recall
    d.running = False  # TestClient's loop is gone; the dialer task can't finish here

    r = tc.delete("/api/campaigns/demo/contacts/12105550101")
    assert r.status_code == 404  # contacts.csv rows are never removed from the dashboard
    r = tc.delete("/api/campaigns/demo/contacts/12105550199")
    assert r.status_code == 200 and not any(x["id"] == "12105550199" for x in r.json()["contacts"])


def test_followup_issues_reflect_server_config(client, monkeypatch):
    tc, main = client
    from app.messaging import FakeMessenger
    shutil.copytree(REPO / "campaigns" / "loyalty", campaigns_mod.CAMPAIGN_DIR / "loyalty")
    monkeypatch.setattr(main.rt, "messenger", FakeMessenger(channels=()))
    msgs = [i["message"] for i in tc.get("/api/campaigns/loyalty").json()["issues"]]
    assert any("Email isn't set up" in m for m in msgs) and any("SMS is off" in m for m in msgs)
    monkeypatch.setattr(main.rt, "messenger", FakeMessenger(channels=("email",)))
    msgs = [i["message"] for i in tc.get("/api/campaigns/loyalty").json()["issues"]]
    assert not any("Email isn't set up" in m for m in msgs) and any("Maya offers email instead" in m for m in msgs)


def test_ai_disclosure_in_opening_can_be_turned_off(camp_dir):
    set_config(camp_dir, opening_line="Hey {first_name}, quick question for you!", disclose_ai_in_opening=False)
    c = load_campaign("demo")
    assert c.opening_for(c.contacts[0]) == "Hey Alex, quick question for you!"
    assert not any("doesn't say the caller is an AI" in i.message for i in c.issues)


def test_manual_call_can_ignore_calling_hours_but_not_consent(camp_dir):
    c = load_campaign("demo")
    set_config(camp_dir, calling_hours={"start": "09:00", "end": "10:00", "timezone": "America/Chicago",
                                        "days": "mon-sun"})
    c = load_campaign("demo")
    results, dnc = ResultStore(c), DoNotCallList(camp_dir / "dnc.txt")
    alex, cy = c.contacts[0], c.contacts[2]
    assert c.eligibility(alex, dnc, results, NOON_MON, recall=True)[1] == "outside calling hours"
    assert c.eligibility(alex, dnc, results, NOON_MON, recall=True, ignore_hours=True) == (True, "ready")
    assert c.eligibility(cy, dnc, results, NOON_MON, recall=True, ignore_hours=True)[1].startswith("no consent")
    plan = {p["name"]: p for p in c.plan(dnc, results, NOON_MON)}
    assert not plan["Alex Garcia"]["can_call"] and plan["Alex Garcia"]["can_call_any_time"]

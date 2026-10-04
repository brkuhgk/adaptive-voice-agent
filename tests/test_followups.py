"""Interest-based follow-ups (SMS/email during the call), data dir on a volume, and the public-server lock."""
import asyncio
import csv
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.campaigns as campaigns_mod
from app.campaigns import CampaignProfile, DoNotCallList, ResultStore, load_campaign
from app.events import EventBus
from app.messaging import FakeMessenger

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def loyalty(tmp_path, monkeypatch):
    root = tmp_path / "campaigns"
    shutil.copytree(REPO / "campaigns" / "loyalty", root / "loyalty")
    (root / "loyalty" / "contacts.csv").write_text(
        "phone,name,consent,email,timezone,voice_id,favorite_item\n"
        "+12105550111,Ana Lopez,yes,ana@example.com,America/Chicago,,cold brew\n"
        "+12105550112,Ben Ito,yes,,America/Chicago,,\n")
    data = tmp_path / "volume"
    monkeypatch.setattr(campaigns_mod, "CAMPAIGN_DIR", root)
    monkeypatch.setenv("DATA_DIR", str(data))
    return root, data


def profile(contact_idx=0, messenger=None, mode="live", bus=None):
    c = load_campaign("loyalty")
    p = CampaignProfile(c, c.contacts[contact_idx], ResultStore(c), DoNotCallList(), mode=mode,
                        messenger=messenger if messenger is not None else FakeMessenger(), bus=bus)
    return c, p, p.new_state("CAfu", None)


def call(p, state, **args):
    return p.execute_tool(state, "send_followup", json.dumps(args))


def test_sample_loyalty_campaign_is_valid():
    c = load_campaign("loyalty")
    assert not [i for i in c.issues if i.level == "error"]
    assert set(c.followups["options"]) == {"home_brewing", "daily_coffee", "food", "events"}


async def test_followup_needs_permission_then_sends_matching_offer_to_own_number(loyalty):
    bus = EventBus()
    fake = FakeMessenger()
    c, p, state = profile(messenger=fake, bus=bus)
    assert "send_followup" in [t["function"]["name"] for t in p.tools]
    prompt = p.system_prompt(state)
    assert "Offers you can send" in prompt and "home_brewing" in prompt and "email we have" in prompt

    assert "Ask the person first" in call(p, state, option="food", channel="sms", permission_given=False)["error"]
    res = call(p, state, option="food", channel="sms", permission_given=True)
    assert res["status"] == "sending"
    await p.wait_for_sends()
    sms = fake.sent[0]
    assert sms["to"] == "+12105550111"  # always the contact's own number
    assert "Ana" in sms["body"] and "offer=pastry" in sms["body"] and "STOP" in sms["body"]

    call(p, state, option="food", channel="email", permission_given=True)
    await p.wait_for_sends()
    email = fake.sent[1]
    assert email["to"] == "ana@example.com" and "pastry" in email["subject"]
    assert "limit" in call(p, state, option="events", channel="email", permission_given=True)["error"]

    row = ResultStore(c).get("12105550111")
    assert row["followups"] == "food:sms:sent; food:email:sent"
    assert (loyalty[1] / "loyalty" / "results.csv").exists()  # results land on the data volume
    assert not (loyalty[0] / "loyalty" / "results.csv").exists()
    assert [e["status"] for e in bus.recent() if e["type"] == "followup"] == ["sent", "sent"]
    assert state.labels["followups"] == ["food by sms (sent)", "food by email (sent)"]


async def test_email_must_be_collected_and_valid_when_none_on_file(loyalty):
    fake = FakeMessenger()
    _, p, state = profile(contact_idx=1, messenger=fake)
    assert "No email on file" in p.system_prompt(state)
    assert "No email on file" in call(p, state, option="events", channel="email", permission_given=True)["error"]
    assert "doesn't look like" in call(p, state, option="events", channel="email", permission_given=True,
                                       email="ben at gmail")["error"]
    assert call(p, state, option="events", channel="email", permission_given=True,
                email="ben@example.com")["ok"]
    await p.wait_for_sends()
    assert fake.sent[0]["to"] == "ben@example.com"


async def test_only_available_channels_are_offered(loyalty):
    _, p, state = profile(messenger=FakeMessenger(channels=("email",)))
    tool = next(t for t in p.tools if t["function"]["name"] == "send_followup")
    assert tool["function"]["parameters"]["properties"]["channel"]["enum"] == ["email"]
    assert "by an email" in p.system_prompt(state)

    _, p2, state2 = profile(messenger=FakeMessenger(channels=()))
    assert "send_followup" not in [t["function"]["name"] for t in p2.tools]
    assert "you cannot send links on this call" in p2.system_prompt(state2)


async def test_failed_send_is_recorded(loyalty):
    c, p, state = profile(messenger=FakeMessenger(fail=True))
    call(p, state, option="daily_coffee", channel="sms", permission_given=True)
    await p.wait_for_sends()
    assert ResultStore(c).get("12105550111")["followups"].startswith("daily_coffee:sms:failed")


def test_text_mode_previews_without_sending(loyalty):
    fake = FakeMessenger()
    _, p, state = profile(messenger=fake, mode="text")
    res = call(p, state, option="home_brewing", channel="email", permission_given=True)
    assert res["status"].startswith("preview") and "beans" in res["message"]["body"] and fake.sent == []


def test_todo_markers_block_a_campaign(loyalty):
    root, _ = loyalty
    path = root / "loyalty" / "content.md"
    path.write_text(path.read_text() + "\n- Price: TODO\n")
    c = load_campaign("loyalty")
    assert any(i.level == "error" and "TODO" in i.message for i in c.issues)


def test_public_server_requires_dashboard_token(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "public_base_url", "https://maya-x.fly.dev")
    monkeypatch.setattr(main.settings, "dashboard_token", "")
    tc = TestClient(main.app)
    assert tc.get("/dashboard").status_code == 403
    assert tc.get("/api/campaigns").status_code == 403
    assert tc.get("/health").status_code == 200  # health stays open for the host's checks
    monkeypatch.setattr(main.settings, "dashboard_token", "pw")
    assert tc.get("/dashboard?token=pw").status_code == 200

"""HTTP surface: Twilio webhook TwiML, outbound consent allowlist, text mode, dashboard auth."""
import re

import pytest
from fastapi.testclient import TestClient

from app import main
from app.call_session import stream_token
from app.llm import ScriptedLLM


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.settings, "public_base_url", "")
    monkeypatch.setattr(main.settings, "dashboard_token", "")
    monkeypatch.setattr(main.settings, "validate_twilio_signature", False)
    return TestClient(main.app)


def test_incoming_call_returns_signed_stream_twiml(client):
    r = client.post("/voice/incoming", data={"CallSid": "CA42", "From": "+12105550100", "Direction": "inbound"},
                    headers={"host": "abc.ngrok-free.app"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert '<Stream url="wss://abc.ngrok-free.app/media-stream">' in r.text
    token = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)
    assert token == stream_token(main.settings.stream_secret, "CA42")
    assert 'name="caller" value="+12105550100"' in r.text


def test_outbound_call_requires_consent_allowlist(client, monkeypatch):
    monkeypatch.setattr(main.settings, "outbound_allowlist", ["+12105550123"])
    r = client.post("/api/call", json={"to": "+19995550000"})
    assert r.status_code == 403 and "ALLOWLIST" in r.json()["detail"]


def test_text_mode_uses_the_same_brain(client, monkeypatch):
    llm = ScriptedLLM([[{"tool": "check_availability", "args": {"visit_type": "annual_physical"}},
                        "I have a few openings next week. Which day works best?"]])
    monkeypatch.setattr(main.rt, "llm", llm)
    monkeypatch.setattr(main.settings, "state_tracker_enabled", False)
    r = client.post("/api/sim/message", json={"text": "I need a physical."})
    body = r.json()
    assert r.status_code == 200 and body["opening"].startswith("Hi, thanks for calling")
    assert "openings" in body["reply"] and body["state"]["offered_slots"]
    r2 = client.post("/api/sim/message", json={"session_id": body["session_id"], "text": "Tuesday"})
    assert r2.json()["session_id"] == body["session_id"]


def test_dashboard_token_is_enforced(client, monkeypatch):
    monkeypatch.setattr(main.settings, "dashboard_token", "s3cret")
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard?token=s3cret").status_code == 200
    assert client.get("/api/appointments").status_code == 401


def test_health_reports_config_problems(client):
    body = client.get("/health").json()
    assert "problems" in body and body["scenario"] == "clinic"

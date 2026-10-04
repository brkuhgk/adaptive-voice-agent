"""FastAPI server: Twilio webhooks + media stream, outbound campaigns, live dashboard, text mode."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import supabase_db
from .call_session import STREAM_PARAMS, CallSession, stream_token
from .campaigns import (Campaign, CampaignProfile, DoNotCallList, ResultStore, add_contact, list_campaigns,
                        load_campaign, remove_added_contact)
from .config import settings
from .dialer import Dialer, TwilioCallPlacer
from .events import bus
from .llm import make_llm
from .messaging import LiveMessenger
from .profiles import AgentProfile, ClinicProfile
from .scenario import load_scenario
from .scheduling import ClinicSchedule
from .stt import DeepgramSTT
from .text_session import TextSession
from .tts import ElevenLabsTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-agent")

STATIC = Path(__file__).resolve().parent / "static"
MACHINE = {"machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}


@dataclass
class CampaignCtx:
    campaign: Campaign
    results: ResultStore


class Runtime:
    def __init__(self) -> None:
        self.scenario = load_scenario(settings.scenario)
        self.schedule = ClinicSchedule.build(self.scenario.timezone, self.scenario.raw.get("demo_records"))
        self.clinic = ClinicProfile(self.scenario, self.schedule)
        self.llm = None
        self.tts = None
        self.problems: list[str] = []
        try:
            self.llm = make_llm(settings)
        except Exception as exc:
            self.problems.append(f"LLM: {exc}")
        try:
            self.tts = ElevenLabsTTS(settings)
        except Exception as exc:
            self.problems.append(f"TTS: {exc}")
        if not settings.deepgram_api_key:
            self.problems.append("STT: DEEPGRAM_API_KEY is not set.")
        self.text_sessions: dict[str, TextSession] = {}
        self.stt_factory = lambda keyterms=None: DeepgramSTT(settings, keyterms=keyterms or [])
        self.dnc = DoNotCallList()
        self.campaigns: dict[str, CampaignCtx] = {}
        self.dialers: dict[str, Dialer] = {}
        self.placer_factory = lambda: TwilioCallPlacer(settings)
        self.messenger = LiveMessenger(settings)
        if settings.is_public and not settings.dashboard_token:
            self.problems.append("DASHBOARD_TOKEN is not set: the dashboard and campaign API are locked "
                                 "because this server is public.")

    # ------------------------------------------------------------ campaigns
    def campaign_ctx(self, name: str, reload: bool = False) -> CampaignCtx:
        """One shared Campaign + ResultStore per campaign, so every writer sees the same rows."""
        if reload or name not in self.campaigns:
            if self.dialer_running(name):
                return self.campaigns[name]  # don't swap files under a running dialer
            camp = load_campaign(name)
            self.campaigns[name] = CampaignCtx(camp, ResultStore(camp))
        return self.campaigns[name]

    def dialer_running(self, name: str) -> bool:
        d = self.dialers.get(name)
        return bool(d and d.running)

    def profile_for_stream(self, params: dict) -> AgentProfile:
        name = params.get("campaign")
        if not name:
            return self.clinic
        ctx = self.campaign_ctx(name)
        contact = ctx.campaign.contact(params.get("contact", ""))
        if contact is None:
            raise ValueError(f"unknown contact {params.get('contact')!r} in campaign {name}")
        return CampaignProfile(ctx.campaign, contact, ctx.results, self.dnc, mode=params.get("mode", "live"),
                               messenger=self.messenger, bus=bus)


rt = Runtime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    for p in rt.problems:
        log.warning("config problem -> %s", p)
    if hasattr(rt.tts, "warm"):  # pre-synthesize the clinic greeting so the first words are instant
        asyncio.create_task(rt.tts.warm(rt.clinic.opening_line, rt.clinic.voice_id))
    log.info("scenario=%s llm=%s model=%s campaigns=%s supabase=%s", rt.scenario.id, settings.llm_provider,
             settings.resolved_llm_model, list_campaigns(), "on" if supabase_db.enabled() else "off")
    yield
    for d in rt.dialers.values():
        d.stop()
    await supabase_db.drain()  # finish saving the last conversations
    if hasattr(rt.tts, "aclose"):
        await rt.tts.aclose()


app = FastAPI(title="Adaptive Voice Agent", lifespan=lifespan)


def _check_dashboard_token(token: str | None) -> None:
    if settings.is_public and not settings.dashboard_token:
        # A public server must never show transcripts or start calls without a password.
        raise HTTPException(status_code=403, detail="Set DASHBOARD_TOKEN on the server to use the dashboard.")
    if settings.dashboard_token and token != settings.dashboard_token:
        raise HTTPException(status_code=401, detail="bad or missing ?token=")


# ------------------------------------------------------------------ health
@app.get("/health")
async def health():
    return {"ok": not rt.problems, "problems": rt.problems, "scenario": rt.scenario.id,
            "llm_provider": settings.llm_provider, "llm_model": settings.resolved_llm_model,
            "campaigns": list_campaigns(), "supabase": supabase_db.enabled()}


# ------------------------------------------------------------------ Twilio
def _twiml_connect(request: Request, call_sid: str, caller: str, **extra: str) -> str:
    from twilio.twiml.voice_response import Connect, VoiceResponse

    base = settings.public_ws_base or f"wss://{request.headers.get('host')}"
    resp = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"{base}/media-stream")
    parts = [extra.get(k, "") for k in STREAM_PARAMS]
    stream.parameter(name="token", value=stream_token(settings.stream_secret, call_sid, *parts))
    stream.parameter(name="caller", value=caller)
    for k in STREAM_PARAMS:
        if extra.get(k):
            stream.parameter(name=k, value=extra[k])
    resp.append(connect)
    return str(resp)


async def _twilio_form(request: Request) -> dict:
    form = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    if settings.validate_twilio_signature:
        from twilio.request_validator import RequestValidator

        url = f"{settings.public_base_url}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"
        sig = request.headers.get("X-Twilio-Signature", "")
        if not RequestValidator(settings.twilio_auth_token).validate(url, form, sig):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
    return form


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


@app.api_route("/voice/incoming", methods=["GET", "POST"])
async def voice_incoming(request: Request):
    """Someone called our number (or /api/call dialed out): talk to the clinic agent."""
    form = await _twilio_form(request)
    call_sid = form.get("CallSid", "unknown")
    outbound = str(form.get("Direction", "")).startswith("outbound")
    caller = form.get("To" if outbound else "From", "")
    log.info("incoming call %s from %s", call_sid, caller)
    return _xml(_twiml_connect(request, call_sid, caller))


@app.api_route("/voice/campaign", methods=["GET", "POST"])
async def voice_campaign(request: Request, c: str, id: str):
    """A campaign call was answered. Twilio has already run answering-machine detection."""
    form = await _twilio_form(request)
    call_sid = form.get("CallSid", "unknown")
    answered_by = form.get("AnsweredBy", "")
    ctx = rt.campaign_ctx(c)
    contact = ctx.campaign.contact(id)
    if contact is None:
        return _xml("<Response><Hangup/></Response>")
    if answered_by in MACHINE:
        if answered_by != "fax" and ctx.campaign.voicemail_for(contact):
            return _xml(_twiml_connect(request, call_sid, contact.phone, campaign=c, contact=id, mode="voicemail"))
        ctx.results.update(contact, outcome="machine_no_message", note=f"answered by {answered_by}")
        return _xml("<Response><Hangup/></Response>")
    return _xml(_twiml_connect(request, call_sid, contact.phone, campaign=c, contact=id, mode="live"))


@app.api_route("/voice/status", methods=["GET", "POST"])
async def voice_status(request: Request, c: str, id: str):
    """Twilio call progress: initiated, ringing, in-progress, completed, busy, no-answer, failed."""
    form = await _twilio_form(request)
    status, sid = form.get("CallStatus", ""), form.get("CallSid", "")
    dialer = rt.dialers.get(c)
    if dialer is not None:
        dialer.on_status(sid, id, status, form.get("AnsweredBy"))
    else:
        ctx = rt.campaign_ctx(c)
        contact = ctx.campaign.contact(id)
        if contact is not None:
            ctx.results.update(contact, call_status=status)
    return Response(status_code=204)


@app.websocket("/media-stream")
async def media_stream(ws: WebSocket):
    await ws.accept()
    session = CallSession(ws, profile_factory=rt.profile_for_stream, llm=rt.llm, tts=rt.tts,
                          stt_factory=rt.stt_factory, bus=bus, settings=settings)
    await session.run()


class OutboundCall(BaseModel):
    to: str


@app.post("/api/call")
async def outbound_call(body: OutboundCall, token: str | None = None):
    """One test call with the clinic agent (number must be on OUTBOUND_ALLOWLIST)."""
    _check_dashboard_token(token)
    if body.to not in settings.outbound_allowlist:
        raise HTTPException(status_code=403, detail="Number is not on OUTBOUND_ALLOWLIST (consenting participants only).")
    if not (settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_phone_number
            and settings.public_base_url):
        raise HTTPException(status_code=400, detail="Set TWILIO_* and PUBLIC_BASE_URL for outbound calls.")
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    call = await asyncio.to_thread(client.calls.create, to=body.to, from_=settings.twilio_phone_number,
                                   url=f"{settings.public_base_url}/voice/incoming")
    return {"call_sid": call.sid}


# ------------------------------------------------------------------ campaigns API
def _ctx_or_404(name: str, reload: bool = False) -> CampaignCtx:
    try:
        return rt.campaign_ctx(name, reload=reload)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"campaign.json is not valid JSON: {exc}")


def _followup_issues(c: Campaign) -> list[dict]:
    """What this server can actually send, so the dashboard doesn't warn about a channel that works."""
    fu = c.followups
    if not fu["options"]:
        return []
    out = []
    email_ok, sms_ok = rt.messenger.can("email"), rt.messenger.can("sms")
    if "email" in fu["channels"] and not email_ok:
        out.append({"level": "warning", "message": "Email isn't set up on the server (EMAIL_PROVIDER, SMTP_* or "
                    "SENDGRID_API_KEY, EMAIL_FROM), so Maya can't email links yet."})
    if "sms" in fu["channels"] and not sms_ok:
        out.append({"level": "info", "message": "SMS is off: US texting needs Twilio Toll-Free Verification or "
                    "A2P 10DLC (paid account). " + ("Maya offers email instead." if email_ok else "")})
    if email_ok:
        out.append({"level": "info", "message": f"Email follow-ups on: sent from {settings.email_from}."})
    return out


def _campaign_view(ctx: CampaignCtx) -> dict:
    c = ctx.campaign
    d = rt.dialers.get(c.name)
    return {
        "name": c.name, "title": c.title, "agent_name": c.agent_name, "org_name": c.org_name,
        "voice_id": c.voice_id or settings.elevenlabs_voice_id, "voice_is_default": not c.voice_id,
        "goal": c.get("goal", ""), "collect": list(c.collect), "hours": c.hours,
        "issues": [i.__dict__ for i in c.issues] + _followup_issues(c), "running": bool(d and d.running),
        "active_calls": len(d.active) if d else 0, "contacts": c.plan(rt.dnc, ctx.results),
    }


@app.get("/api/campaigns")
async def campaigns_index(token: str | None = None):
    _check_dashboard_token(token)
    out = []
    for name in list_campaigns():
        try:
            ctx = _ctx_or_404(name, reload=not rt.dialer_running(name))
            out.append({"name": name, "title": ctx.campaign.title, "contacts": len(ctx.campaign.contacts),
                        "running": rt.dialer_running(name)})
        except HTTPException as exc:
            out.append({"name": name, "error": exc.detail})
    return out


@app.get("/api/campaigns/{name}")
async def campaign_detail(name: str, token: str | None = None):
    """Re-reads the campaign files, so edits show up right away."""
    _check_dashboard_token(token)
    return _campaign_view(_ctx_or_404(name, reload=not rt.dialer_running(name)))


def _start_dialer(name: str, only: set[str] | None = None, ignore_hours: bool = False) -> dict:
    """Start calling. only=None is the normal run (new contacts + retries); a set of contact ids is a manual
    run from the dashboard that calls exactly those people, again if needed (and, with ignore_hours,
    outside calling hours)."""
    if rt.dialer_running(name):
        raise HTTPException(status_code=409, detail="calls are already in progress; wait for them or press Stop")
    ctx = _ctx_or_404(name, reload=True)
    errors = [i.message for i in ctx.campaign.issues if i.level == "error"]
    if errors:
        raise HTTPException(status_code=400, detail="fix these first: " + "; ".join(errors))
    plan = ctx.campaign.plan(rt.dnc, ctx.results)
    if only is not None:
        unknown = only - {p["id"] for p in plan}
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown contact(s): {', '.join(sorted(unknown))}")
        chosen = [p for p in plan if p["id"] in only]
        ok = lambda p: p["can_call_any_time"] if ignore_hours else p["can_call"]  # noqa: E731
        will_call = sum(1 for p in chosen if ok(p))
        if not will_call:
            hint = (" (tick 'Call outside calling hours' to call anyway)"
                    if any(p["can_call_any_time"] for p in chosen) else "")
            raise HTTPException(status_code=400, detail="can't call: " + "; ".join(
                f"{p['name'] or p['phone']}: {p['call_reason']}" for p in chosen) + hint)
    else:
        will_call = sum(1 for p in plan if p["eligible"])
    if rt.llm is None or rt.tts is None:
        raise HTTPException(status_code=400, detail="LLM and TTS must be configured; see /health")
    try:
        placer = rt.placer_factory()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    warm = getattr(rt.tts, "warm", None)
    dialer = Dialer(ctx.campaign, ctx.results, rt.dnc, placer, bus, warm=warm)
    rt.dialers[name] = dialer
    plan = dialer.start(only=only, recall=only is not None, ignore_hours=ignore_hours and only is not None)
    skipped = [f"{p['name'] or p['phone']}: {p['call_reason']}" for p in plan
               if only is not None and p["id"] in only
               and not (p["can_call_any_time"] if ignore_hours else p["can_call"])]
    return {"started": True, "will_call": will_call, "skipped": skipped, "plan": plan}


@app.post("/api/campaigns/{name}/start")
async def campaign_start(name: str, token: str | None = None):
    _check_dashboard_token(token)
    return _start_dialer(name)


class CallRequest(BaseModel):
    contact_ids: list[str]
    ignore_hours: bool = False


@app.post("/api/campaigns/{name}/call")
async def campaign_call(name: str, body: CallRequest, token: str | None = None):
    """Call the chosen contacts now (one or many), even if they were already called."""
    _check_dashboard_token(token)
    if not body.contact_ids:
        raise HTTPException(status_code=400, detail="choose at least one contact")
    return _start_dialer(name, only=set(body.contact_ids), ignore_hours=body.ignore_hours)


class NewContact(BaseModel):
    phone: str
    name: str = ""
    email: str = ""
    consent: bool = False
    call_now: bool = False
    ignore_hours: bool = False


@app.post("/api/campaigns/{name}/contacts")
async def campaign_add_contact(name: str, body: NewContact, token: str | None = None):
    """Add a person from the dashboard (saved on the data volume, so a redeploy keeps it)."""
    _check_dashboard_token(token)
    if not body.consent:
        raise HTTPException(status_code=400, detail="only add people who agreed to get this call (tick the box)")
    if rt.dialer_running(name):
        raise HTTPException(status_code=409, detail="wait for the current calls to finish, or press Stop")
    ctx = _ctx_or_404(name, reload=True)
    try:
        contact = add_contact(ctx.campaign, body.phone, body.name, body.email, rt.dnc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    out: dict = {"added": contact.id}
    if body.call_now:
        try:
            started = _start_dialer(name, only={contact.id}, ignore_hours=body.ignore_hours)
            out["call"] = {k: v for k, v in started.items() if k != "plan"}
        except HTTPException as exc:
            out["call_error"] = exc.detail
    return {**out, "campaign": _campaign_view(_ctx_or_404(name, reload=not rt.dialer_running(name)))}


@app.delete("/api/campaigns/{name}/contacts/{contact_id}")
async def campaign_remove_contact(name: str, contact_id: str, token: str | None = None):
    """Remove someone added from the dashboard (people in contacts.csv are edited in the file)."""
    _check_dashboard_token(token)
    if rt.dialer_running(name):
        raise HTTPException(status_code=409, detail="wait for the current calls to finish, or press Stop")
    ctx = _ctx_or_404(name, reload=True)
    if not remove_added_contact(ctx.campaign, contact_id):
        raise HTTPException(status_code=404, detail="no dashboard-added contact with that id")
    return _campaign_view(_ctx_or_404(name, reload=True))


@app.post("/api/campaigns/{name}/stop")
async def campaign_stop(name: str, token: str | None = None):
    _check_dashboard_token(token)
    d = rt.dialers.get(name)
    if not d or not d.running:
        return {"stopped": False, "detail": "not running"}
    d.stop()
    return {"stopped": True, "active_calls_finishing": len(d.active)}


class CampaignPatch(BaseModel):
    voice_id: str | None = None


@app.patch("/api/campaigns/{name}")
async def campaign_patch(name: str, body: CampaignPatch, token: str | None = None):
    """Change settings that are safe to edit from the dashboard (today: the voice)."""
    _check_dashboard_token(token)
    if rt.dialer_running(name):
        raise HTTPException(status_code=409, detail="stop the campaign before changing it")
    ctx = _ctx_or_404(name, reload=True)
    path = ctx.campaign.path / "campaign.json"
    config = json.loads(path.read_text())
    if body.voice_id is not None:
        config["voice_id"] = body.voice_id.strip()
    path.write_text(json.dumps(config, indent=2) + "\n")
    return _campaign_view(_ctx_or_404(name, reload=True))


@app.get("/api/voices")
async def voices(token: str | None = None):
    _check_dashboard_token(token)
    if not hasattr(rt.tts, "list_voices"):
        raise HTTPException(status_code=400, detail="ElevenLabs is not configured")
    try:
        return {"default": settings.elevenlabs_voice_id, "voices": await rt.tts.list_voices()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ElevenLabs: {exc}")


# ------------------------------------------------------------------ dashboard
@app.get("/")
@app.get("/dashboard")
async def dashboard(token: str | None = None):
    _check_dashboard_token(token)
    return FileResponse(STATIC / "dashboard.html")


@app.get("/events")
async def events(request: Request, token: str | None = None):
    _check_dashboard_token(token)
    queue = bus.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            for e in bus.recent():
                yield f"data: {json.dumps(e, default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(e, default=str)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/appointments")
async def appointments(token: str | None = None):
    _check_dashboard_token(token)
    today = rt.schedule.today()
    return [a.to_dict(today) for a in sorted(rt.schedule.appointments.values(), key=lambda a: a.start)]


@app.get("/api/info")
async def info(token: str | None = None):
    _check_dashboard_token(token)
    return {"scenario": rt.scenario.raw["title"], "org": rt.scenario.org["name"], "agent": rt.scenario.agent["name"],
            "phone": settings.twilio_phone_number, "problems": rt.problems,
            "llm": f"{settings.llm_provider} / {settings.resolved_llm_model}"}


class SimMessage(BaseModel):
    session_id: str | None = None
    text: str
    campaign: str | None = None      # test a campaign in text mode (nothing is saved)
    contact_id: str | None = None


@app.post("/api/sim/message")
async def sim_message(body: SimMessage, token: str | None = None):
    """Type as the other person. Same brain, no phone/STT/TTS required."""
    _check_dashboard_token(token)
    if rt.llm is None:
        raise HTTPException(status_code=400, detail="LLM is not configured; see /health")
    session = rt.text_sessions.get(body.session_id or "")
    opening = None
    if session is None or session.ended:
        if body.campaign:
            try:
                camp = load_campaign(body.campaign)  # fresh read: test your latest edits
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            contact = camp.contact(body.contact_id or "") or (camp.contacts[0] if camp.contacts else None)
            if contact is None:
                raise HTTPException(status_code=400, detail="contacts.csv has no rows")
            profile: AgentProfile = CampaignProfile(camp, contact, ResultStore(camp), DoNotCallList(), mode="text")
        else:
            profile = rt.clinic
        session = TextSession(profile, rt.llm, bus, settings)
        rt.text_sessions[session.id] = session
        opening = session.opening_line
        if not body.text.strip():
            return JSONResponse({"session_id": session.id, "reply": opening, "state": session.state.to_dict()})
    reply = await session.say(body.text)
    return {"session_id": session.id, "opening": opening, "reply": reply, "state": session.state.to_dict(),
            "ended": session.ended}

"""The dialer walks a campaign's contact list and places calls through Twilio.

    for each contact in contacts.csv (top to bottom):
        skip it unless: valid number, consent=yes, not on do-not-call list,
                        not already done, attempts left, inside calling hours
        wait until fewer than max_concurrent_calls are active
        place the call (Twilio rings the phone; when answered, Twilio opens /media-stream)
    wait for the last calls to finish

A manual run (start(only=..., recall=True), from the dashboard's Call buttons) walks only the chosen
contacts and calls them again even if they are done or out of attempts.

Twilio reports progress (ringing, in-progress, completed, busy, no-answer...) to /voice/status,
which lands in Dialer.on_status. The call's outcome (what Maya decided) is written by the
CampaignProfile when the call ends.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.parse import urlencode

from .campaigns import Campaign, Contact, DoNotCallList, ResultStore, mask
from .config import Settings
from .events import EventBus

log = logging.getLogger(__name__)

TERMINAL = {"completed", "busy", "failed", "no-answer", "canceled"}


class CallPlacer(Protocol):
    async def place(self, campaign: Campaign, contact: Contact) -> str: ...


class TwilioCallPlacer:
    def __init__(self, settings: Settings):
        missing = [k for k in ("twilio_account_sid", "twilio_auth_token", "twilio_phone_number", "public_base_url")
                   if not getattr(settings, k)]
        if missing:
            raise RuntimeError(f"Set {', '.join(m.upper() for m in missing)} in .env to place calls.")
        from twilio.rest import Client

        self.settings = settings
        self.client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    async def place(self, campaign: Campaign, contact: Contact) -> str:
        base = self.settings.public_base_url
        q = urlencode({"c": campaign.name, "id": contact.id})
        kwargs: dict[str, Any] = {
            "to": contact.phone, "from_": self.settings.twilio_phone_number,
            "url": f"{base}/voice/campaign?{q}", "method": "POST",
            "status_callback": f"{base}/voice/status?{q}", "status_callback_method": "POST",
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "timeout": int(campaign.get("ring_timeout_seconds", 25)),
        }
        if campaign.get("answering_machine", "detect") == "detect":
            # DetectMessageEnd waits for the beep, so a voicemail lands after the greeting.
            kwargs["machine_detection"] = "DetectMessageEnd" if campaign.get("voicemail_message") else "Enable"
        call = await asyncio.to_thread(self.client.calls.create, **kwargs)
        return call.sid


class Dialer:
    def __init__(self, campaign: Campaign, results: ResultStore, dnc: DoNotCallList, placer: CallPlacer,
                 bus: EventBus, warm: Callable[[str, str | None], Any] | None = None,
                 clock: Callable[[], datetime] | None = None, gap_s: float = 2.0):
        self.campaign = campaign
        self.results = results
        self.dnc = dnc
        self.placer = placer
        self.bus = bus
        self.warm = warm
        self.clock = clock
        self.gap_s = gap_s
        self.active: dict[str, tuple[str, float]] = {}  # call_sid -> (contact_id, started)
        self.running = False
        self.stopping = False
        self.placed = 0
        self.only: set[str] | None = None
        self.recall = False
        self.ignore_hours = False
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None

    def _publish(self, type_: str, **data: Any) -> None:
        self.bus.publish(f"campaign:{self.campaign.name}", type_, campaign=self.campaign.name, **data)

    def start(self, only: set[str] | None = None, recall: bool = False,
              ignore_hours: bool = False) -> list[dict[str, Any]]:
        if self.running:
            raise RuntimeError("this campaign is already running")
        plan = self.campaign.plan(self.dnc, self.results, self.clock() if self.clock else None)
        self.running, self.stopping, self.placed = True, False, 0
        self.only, self.recall, self.ignore_hours = only, recall, ignore_hours
        self._task = asyncio.create_task(self._run())
        return plan

    def stop(self) -> None:
        """Stop placing new calls. Calls already in progress finish normally."""
        self.stopping = True
        self._changed.set()

    async def wait(self) -> None:
        if self._task:
            await self._task

    async def _run(self) -> None:
        contacts = [c for c in self.campaign.contacts if self.only is None or c.id in self.only]
        self._publish("campaign_started", contacts=len(contacts), manual=self.recall)
        seen: set[str] = set()
        try:
            for contact in contacts:
                if self.stopping:
                    break
                ok, reason = self.campaign.eligibility(contact, self.dnc, self.results,
                                                       self.clock() if self.clock else None, seen,
                                                       recall=self.recall, ignore_hours=self.ignore_hours)
                if not ok:
                    self._publish("campaign_skip", contact_id=contact.id, name=contact.name, reason=reason)
                    continue
                await self._wait_for_slot()
                if self.stopping:
                    break
                if self.dnc.contains(contact.phone):  # may have changed while we waited
                    continue
                await self._dial(contact)
                await asyncio.sleep(self.gap_s)
            deadline = time.monotonic() + self._call_ttl()
            while self.active and time.monotonic() < deadline:
                await self._wait_change(2)
                self._expire_stale()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("dialer crashed")
            self._publish("campaign_error", detail=str(exc))
        finally:
            self.running = False
            self._publish("campaign_finished", placed=self.placed, stopped=self.stopping)

    async def _dial(self, contact: Contact) -> None:
        row = self.results.get(contact.id) or {}
        attempts = int(row.get("attempts") or 0) + 1
        opening = self.campaign.opening_for(contact)
        if self.warm is not None:  # synthesize the greeting while the phone rings
            asyncio.create_task(self.warm(opening, contact.voice_id or self.campaign.voice_id))
        try:
            sid = await self.placer.place(self.campaign, contact)
        except Exception as exc:
            log.warning("could not place call to %s: %s", mask(contact.phone), exc)
            self.results.update(contact, call_status="failed", attempts=attempts, note=f"dial error: {exc}")
            self._publish("campaign_status", contact_id=contact.id, name=contact.name, call_status="failed",
                          note=str(exc))
            return
        self.placed += 1
        self.active[sid] = (contact.id, time.monotonic())
        self.results.update(contact, call_status="dialing", attempts=attempts, call_sid=sid, note="", outcome="")
        self._publish("campaign_status", contact_id=contact.id, name=contact.name, call_status="dialing",
                      call_sid=sid, attempts=attempts)

    def on_status(self, call_sid: str, contact_id: str, status: str, answered_by: str | None = None) -> None:
        contact = self.campaign.contact(contact_id)
        if contact is not None:
            note = f"answered by {answered_by}" if answered_by else None
            self.results.update(contact, call_status=status, note=note)
            self._publish("campaign_status", contact_id=contact_id, name=contact.name, call_status=status,
                          call_sid=call_sid)
        if status in TERMINAL:
            self.active.pop(call_sid, None)
            self._changed.set()

    # ------------------------------------------------------------ helpers
    def _call_ttl(self) -> float:
        return int(self.campaign.get("ring_timeout_seconds", 25)) + int(self.campaign.get("max_call_seconds", 300)) + 90

    def _expire_stale(self) -> None:
        """Forget calls whose status callback never arrived (e.g. tunnel down)."""
        now = time.monotonic()
        for sid, (cid, started) in list(self.active.items()):
            if now - started > self._call_ttl():
                self.active.pop(sid, None)
                self._publish("campaign_status", contact_id=cid, call_sid=sid, call_status="unknown",
                              note="no status from Twilio")

    async def _wait_change(self, timeout: float) -> None:
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _wait_for_slot(self) -> None:
        while len(self.active) >= self.campaign.max_concurrent and not self.stopping:
            await self._wait_change(2)
            self._expire_stale()

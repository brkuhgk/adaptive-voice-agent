"""Follow-up messages sent during a call: SMS through Twilio, email through SendGrid (Twilio) or SMTP.

Rules that keep this safe:
  - SMS only goes to the contact's own phone number from contacts.csv, never to a number said on the call.
  - The agent must ask permission first (the tool requires `permission_given=true`).
  - Each campaign sets how many follow-ups one call may send.
  - Sending runs in the background, so the conversation never waits for it.

US carriers block SMS from unverified numbers: toll-free numbers need Toll-Free Verification and
local numbers need A2P 10DLC registration (both need a paid Twilio account). Email has no such wait.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class Messenger(Protocol):
    def can(self, channel: str) -> bool: ...

    async def send_sms(self, to: str, body: str) -> dict[str, Any]: ...

    async def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]: ...


class LiveMessenger:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=15)

    def can(self, channel: str) -> bool:
        s = self.s
        if channel == "sms":
            return bool(s.sms_enabled and s.twilio_account_sid and s.twilio_auth_token
                        and (s.sms_from or s.twilio_messaging_service_sid))
        if channel == "email":
            if s.email_provider == "sendgrid":
                return bool(s.sendgrid_api_key and s.email_from)
            if s.email_provider == "smtp":
                return bool(s.smtp_host and s.email_from)
        return False

    async def send_sms(self, to: str, body: str) -> dict[str, Any]:
        s = self.s
        data = {"To": to, "Body": body}
        if s.twilio_messaging_service_sid:
            data["MessagingServiceSid"] = s.twilio_messaging_service_sid
        else:
            data["From"] = s.sms_from
        url = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Messages.json"
        r = await self.client.post(url, data=data, auth=(s.twilio_account_sid, s.twilio_auth_token))
        body_json = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400:
            return {"ok": False, "error": f"Twilio {r.status_code} {body_json.get('code', '')}: "
                                          f"{body_json.get('message', r.text[:200])}"}
        return {"ok": True, "id": body_json.get("sid"), "status": body_json.get("status")}

    async def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        s = self.s
        if s.email_provider == "sendgrid":
            payload = {
                "personalizations": [{"to": [{"email": to}]}],
                "from": {"email": s.email_from, "name": s.email_from_name or s.email_from},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            }
            r = await self.client.post("https://api.sendgrid.com/v3/mail/send", json=payload,
                                       headers={"Authorization": f"Bearer {s.sendgrid_api_key}"})
            if r.status_code >= 400:
                return {"ok": False, "error": f"SendGrid {r.status_code}: {r.text[:200]}"}
            return {"ok": True, "id": r.headers.get("x-message-id")}
        if s.email_provider == "smtp":
            return await asyncio.to_thread(self._smtp, to, subject, body)
        return {"ok": False, "error": "EMAIL_PROVIDER is not set"}

    def _smtp(self, to: str, subject: str, body: str) -> dict[str, Any]:
        s = self.s
        msg = EmailMessage()
        msg["From"] = f"{s.email_from_name} <{s.email_from}>" if s.email_from_name else s.email_from
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": f"SMTP: {exc}"}


class FakeMessenger:
    """Test double (and text mode): records messages instead of sending them."""

    def __init__(self, channels: tuple[str, ...] = ("sms", "email"), fail: bool = False):
        self.channels = channels
        self.fail = fail
        self.sent: list[dict[str, Any]] = []

    def can(self, channel: str) -> bool:
        return channel in self.channels

    async def send_sms(self, to: str, body: str) -> dict[str, Any]:
        self.sent.append({"channel": "sms", "to": to, "body": body})
        return {"ok": not self.fail, **({"error": "fake failure"} if self.fail else {"id": f"SM{len(self.sent)}"})}

    async def send_email(self, to: str, subject: str, body: str) -> dict[str, Any]:
        self.sent.append({"channel": "email", "to": to, "subject": subject, "body": body})
        return {"ok": not self.fail, **({"error": "fake failure"} if self.fail else {"id": f"EM{len(self.sent)}"})}

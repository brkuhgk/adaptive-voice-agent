"""Supabase: campaign contacts come from the signup_requests table, and every call is saved to
call_conversations (one row per call, with the full transcript). See supabase/schema.sql.

Off unless SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set. When it is off, or Supabase is down,
everything still works from the CSV files as before: Supabase is added on top, never required.

We use the PostgREST HTTP API directly (httpx is already a dependency), with the service_role key.
That key bypasses row-level security, so it must only ever live on the server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from .config import settings

log = logging.getLogger(__name__)

SIGNUPS_TABLE = "signup_requests"
CONVERSATIONS_TABLE = "call_conversations"
CACHE_TTL_S = 10  # the dashboard re-reads the campaign often; don't hit Supabase on every refresh
TIMEOUT_S = 4

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_tasks: set[asyncio.Task] = set()
_transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None  # tests swap in a fake PostgREST


class SupabaseError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(settings.supabase_url and settings.supabase_key)


def _headers(**extra: str) -> dict[str, str]:
    key = settings.supabase_key
    h = {"apikey": key, "Content-Type": "application/json", **extra}
    if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; new sb_secret_ keys must not
        h["Authorization"] = f"Bearer {key}"
    return h


def _url(table: str) -> str:
    return f"{settings.supabase_url.rstrip('/')}/rest/v1/{table}"


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


# ---------------------------------------------------------------- contacts (read)
def fetch_signups(table: str = SIGNUPS_TABLE, order: str = "consented_at.asc") -> list[dict[str, Any]]:
    """All rows of the sign-up table, oldest first. Cached for a few seconds.
    Raises SupabaseError; if a cached copy exists it is returned instead (so a blip doesn't empty the list)."""
    hit = _cache.get(table)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        with httpx.Client(transport=_transport, timeout=TIMEOUT_S) as client:
            r = client.get(_url(table), headers=_headers(), params={"select": "*", **({"order": order} if order else {})})
        r.raise_for_status()
        rows = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        detail = getattr(getattr(exc, "response", None), "text", "") or str(exc)
        if hit:
            log.warning("supabase: %s unreachable, using cached rows: %s", table, detail[:200])
            return hit[1]
        raise SupabaseError(f"couldn't read {table}: {detail[:200]}") from exc
    _cache[table] = (time.monotonic(), rows)
    return rows


def clear_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------- writes (background)
def _spawn(coro) -> None:
    """Fire and forget from sync code (on_call_end). Writes never delay or break a call."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    task = loop.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _request(method: str, table: str, *, params: dict[str, str] | None = None,
                   body: Any = None, prefer: str = "return=minimal") -> bool:
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=10) as client:
            r = await client.request(method, _url(table), headers=_headers(Prefer=prefer), params=params,
                                     content=json.dumps(body, default=str))
        if r.status_code >= 300:
            log.warning("supabase: %s %s -> %s %s", method, table, r.status_code, r.text[:300])
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("supabase: %s %s failed: %s", method, table, exc)
        return False


async def _save_conversation(build: Callable[[], dict[str, Any]],
                             before: Callable[[], Awaitable[Any]] | None) -> None:
    if before is not None:  # e.g. wait for follow-up texts/emails to report sent/failed
        try:
            await before()
        except Exception:
            log.exception("supabase: pre-save hook failed")
    record = build()
    if await _request("POST", CONVERSATIONS_TABLE, params={"on_conflict": "call_sid"}, body=record,
                      prefer="resolution=merge-duplicates,return=minimal"):
        log.info("supabase: saved conversation %s (%s)", record.get("call_sid"), record.get("outcome"))


def save_conversation(build: Callable[[], dict[str, Any]], before: Callable[[], Awaitable[Any]] | None = None) -> None:
    """Upsert one call into call_conversations (keyed by call_sid), in the background.
    build() makes the row; it runs after before() finishes, so the row has the final values."""
    if enabled():
        _spawn(_save_conversation(build, before))


def revoke_signup(signup_id: str, table: str = SIGNUPS_TABLE) -> None:
    """The person opted out on the call: mark their sign-up revoked so no system calls them again."""
    if enabled() and signup_id:
        _cache.pop(table, None)
        _spawn(_request("PATCH", table, params={"id": f"eq.{signup_id}"}, body={"status": "revoked"}))


async def drain(timeout: float = 15) -> None:
    """Wait for pending writes (used on shutdown and in tests)."""
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=timeout)


def conversation_record(state: Any, *, kind: str, outcome: str | None, **extra: Any) -> dict[str, Any]:
    """The call_conversations row for a finished call. extra fills campaign/contact columns."""
    return {
        "call_sid": state.call_id, "kind": kind, "outcome": outcome, "summary": state.summary,
        "callback_time": state.callback_time, "fields": {k: v for k, v in state.fields.items() if v},
        "transcript": state.transcript, "state": state.to_dict(), "turns": state.turns,
        "duration_s": round(state.elapsed_s), "started_at": _iso(state.started_at), "ended_at": _iso(time.time()),
        "phone": state.caller_number, **extra,
    }

"""Preflight check: verifies every key and measures provider latency before you demo.

    python -m scripts.check_setup                   # check everything
    python -m scripts.check_setup --voices          # also list ElevenLabs voices you can use
    python -m scripts.check_setup --configure-twilio  # point your Twilio number at PUBLIC_BASE_URL
"""
from __future__ import annotations

import argparse
import asyncio
import time

import httpx

from app.config import settings
from app.llm import make_llm

OK, BAD, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"


async def check_llm() -> None:
    print(f"\nLLM  ({settings.llm_provider} → {settings.resolved_llm_base_url}, model {settings.resolved_llm_model})")
    try:
        llm = make_llm(settings)
        t0 = time.perf_counter()
        first = None
        text = ""
        async for d in llm.stream_chat([{"role": "user", "content": "Reply with exactly: ready"}]):
            if d.text:
                first = first or time.perf_counter()
                text += d.text
        print(f"  {OK} reply {text.strip()!r}, first token {1000 * (first - t0):.0f} ms" if first
              else f"  {WARN} empty reply")
        tool = [{"type": "function", "function": {"name": "ping", "description": "Call this.",
                                                  "parameters": {"type": "object", "properties": {}}}}]
        names = [d.tool_name async for d in llm.stream_chat([{"role": "user", "content": "Call the ping tool."}], tool)
                 if d.tool_name]
        print(f"  {OK} tool calling works" if names else f"  {WARN} model did not call the tool; pick a tool-capable model")
        data = await llm.complete_json([{"role": "user", "content": 'Return JSON {"ok": true}'}])
        print(f"  {OK} JSON mode works" if data.get("ok") else f"  {WARN} JSON mode returned {data}")
    except Exception as exc:
        print(f"  {BAD} {exc}")


async def check_deepgram(client: httpx.AsyncClient) -> None:
    print("\nDeepgram (speech-to-text)")
    if not settings.deepgram_api_key:
        print(f"  {BAD} DEEPGRAM_API_KEY missing")
        return
    r = await client.get("https://api.deepgram.com/v1/projects",
                         headers={"Authorization": f"Token {settings.deepgram_api_key}"})
    print(f"  {OK} key valid, model {settings.deepgram_model}" if r.status_code == 200
          else f"  {BAD} HTTP {r.status_code}: {r.text[:200]}")


async def check_elevenlabs(client: httpx.AsyncClient, list_voices: bool) -> None:
    print(f"\nElevenLabs (text-to-speech, model {settings.elevenlabs_model}, voice {settings.elevenlabs_voice_id})")
    if not settings.elevenlabs_api_key:
        print(f"  {BAD} ELEVENLABS_API_KEY missing")
        return
    headers = {"xi-api-key": settings.elevenlabs_api_key}
    t0 = time.perf_counter()
    first = None
    size = 0
    async with client.stream("POST", f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}/stream",
                             params={"output_format": "ulaw_8000"}, headers=headers,
                             json={"text": "Hi, this is a test.", "model_id": settings.elevenlabs_model}) as r:
        if r.status_code != 200:
            print(f"  {BAD} HTTP {r.status_code}: {(await r.aread())[:300]!r}")
            return
        async for chunk in r.aiter_bytes():
            first = first or time.perf_counter()
            size += len(chunk)
    print(f"  {OK} {size} bytes of 8 kHz mu-law ({size / 8000:.1f}s audio), first byte {1000 * (first - t0):.0f} ms")
    if list_voices:
        r = await client.get("https://api.elevenlabs.io/v1/voices", headers=headers)
        if r.status_code == 200:
            for v in r.json().get("voices", [])[:25]:
                print(f"    {v['voice_id']}  {v['name']}  ({v.get('category')})")
        else:
            print(f"  {WARN} could not list voices: HTTP {r.status_code}")


async def check_twilio(configure: bool) -> None:
    print("\nTwilio (phone)")
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        print(f"  {BAD} TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing")
        return
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    try:
        acct = await asyncio.to_thread(client.api.accounts(settings.twilio_account_sid).fetch)
        print(f"  {OK} account {acct.friendly_name} ({acct.type})")
        if acct.type == "Trial":
            print(f"  {WARN} trial account: callers hear a short trial message first, and outbound calls"
                  " only reach verified numbers")
        numbers = await asyncio.to_thread(client.incoming_phone_numbers.list,
                                          phone_number=settings.twilio_phone_number or None, limit=5)
        if not numbers:
            print(f"  {BAD} TWILIO_PHONE_NUMBER {settings.twilio_phone_number!r} not found on this account")
            return
        want = f"{settings.public_base_url}/voice/incoming" if settings.public_base_url else None
        for n in numbers:
            status = OK if want and n.voice_url == want else WARN
            print(f"  {status} {n.phone_number} voice webhook = {n.voice_url or '(none)'}")
            if configure and want and n.voice_url != want:
                await asyncio.to_thread(n.update, voice_url=want, voice_method="POST")
                print(f"  {OK} set {n.phone_number} voice webhook → {want}")
        if not want:
            print(f"  {WARN} PUBLIC_BASE_URL not set (your ngrok/cloudflared https URL)")
    except Exception as exc:
        print(f"  {BAD} {exc}")


async def check_public_url(client: httpx.AsyncClient) -> None:
    print("\nPublic URL (Twilio must reach this server)")
    if not settings.public_base_url:
        print(f"  {WARN} PUBLIC_BASE_URL not set")
        return
    try:
        r = await client.get(f"{settings.public_base_url}/health", headers={"ngrok-skip-browser-warning": "1"})
        body = r.json()
        print(f"  {OK} {settings.public_base_url} reachable; server reports ok={body.get('ok')} {body.get('problems') or ''}")
    except Exception as exc:
        print(f"  {BAD} {settings.public_base_url}/health not reachable ({exc}). Is the server and tunnel running?")


async def main(args) -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        await check_llm()
        await check_deepgram(client)
        await check_elevenlabs(client, args.voices)
        await check_twilio(args.configure_twilio)
        await check_public_url(client)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--voices", action="store_true")
    ap.add_argument("--configure-twilio", action="store_true")
    asyncio.run(main(ap.parse_args()))

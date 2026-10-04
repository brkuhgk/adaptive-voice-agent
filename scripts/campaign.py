"""Manage outbound campaigns from the terminal.

    python -m scripts.campaign new <name>                  make campaigns/<name>/ from the template
    python -m scripts.campaign validate <name>             check the files; show who will be called and why not
    python -m scripts.campaign chat <name> [--contact ID]  talk to Maya in text, as one contact (nothing is saved)
    python -m scripts.campaign voices                      list ElevenLabs voices you can use
    python -m scripts.campaign set-voice <name> <voice_id> set the campaign voice ("" = default voice)
    python -m scripts.campaign start <name>                start calling (the server must be running)
    python -m scripts.campaign status <name>               show progress
    python -m scripts.campaign stop <name>                 stop placing new calls
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys

import httpx

from app.campaigns import CAMPAIGN_DIR, CampaignProfile, DoNotCallList, ResultStore, load_campaign
from app.config import settings

RED, YEL, GRN, DIM, BOLD, CYAN, RST = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[1m", "\033[36m", "\033[0m"


def _vis(text: str) -> int:
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def table(rows: list[dict], cols: list[str]) -> None:
    widths = {c: max([len(c), *(_vis(str(r.get(c, ""))) for r in rows)]) for c in cols}
    print("  ".join(f"{BOLD}{c}{RST}" + " " * (widths[c] - len(c)) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")) + " " * (widths[c] - _vis(str(r.get(c, "")))) for c in cols))


def cmd_new(args) -> None:
    dest = CAMPAIGN_DIR / args.name
    if dest.exists():
        sys.exit(f"{dest} already exists")
    src = CAMPAIGN_DIR / "demo"
    dest.mkdir(parents=True)
    for f in ("campaign.json", "instructions.md", "content.md"):
        shutil.copy(src / f, dest / f)
    (dest / "contacts.csv").write_text("phone,name,consent,timezone,voice_id\n+15555550100,Test Person,no,America/Chicago,\n")
    print(f"{GRN}Created {dest}{RST}. Next: edit the 4 files, then run: python -m scripts.campaign validate {args.name}")


def cmd_validate(args) -> None:
    c = load_campaign(args.name)
    print(f"{BOLD}{c.title}{RST}  ({c.path})")
    print(f"agent: {c.agent_name} for {c.org_name}   voice: {c.voice_id or 'default (' + settings.elevenlabs_voice_id + ')'}")
    print(f"calling hours: {c.hours['start']}-{c.hours['end']} {c.hours['days']} ({c.hours['timezone']})\n")
    if not c.issues:
        print(f"{GRN}No problems found.{RST}\n")
    for i in c.issues:
        print(f"{RED if i.level == 'error' else YEL}{i.level:>7}{RST}  {i.message}")
    print()
    plan = c.plan(DoNotCallList(), ResultStore(c))
    for p in plan:
        p["will_call"] = f"{GRN}yes{RST}" if p["eligible"] else f"{DIM}no{RST}"
    table(plan, ["id", "name", "phone", "will_call", "reason", "outcome", "attempts"])
    n = sum(p["eligible"] for p in plan)
    print(f"\n{n} of {len(plan)} contacts would be called now.")
    if any(i.level == "error" for i in c.issues):
        sys.exit(1)


async def cmd_chat(args) -> None:
    from app.events import EventBus
    from app.llm import make_llm
    from app.text_session import TextSession

    c = load_campaign(args.name)
    contact = c.contact(args.contact) if args.contact else (c.contacts[0] if c.contacts else None)
    if contact is None:
        sys.exit("no such contact")
    profile = CampaignProfile(c, contact, ResultStore(c), DoNotCallList(), mode="text")
    bus = EventBus()
    q = bus.subscribe()
    session = TextSession(profile, make_llm(settings), bus, settings)
    print(f"{DIM}Text test as {contact.name} ({contact.id}). Nothing is saved. Ctrl+C to quit.{RST}")
    print(f"{BOLD}{CYAN}{c.agent_name}:{RST} {session.opening_line}")
    while not session.ended:
        try:
            text = await asyncio.to_thread(input, f"{BOLD}{contact.first_name}:{RST} ")
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip():
            continue
        reply = await session.say(text)
        while not q.empty():
            e = q.get_nowait()
            if e["type"] == "tool_call":
                print(f"{YEL}  ⚙ {e['name']}({json.dumps(e['args'])}){RST}")
        print(f"{BOLD}{CYAN}{c.agent_name}:{RST} {reply}")
        s = session.state
        print(f"{DIM}  collected={ {k: v for k, v in s.fields.items() if v} } missing={s.missing_required}{RST}")
    s = session.state
    print(f"{DIM}(ended) outcome={s.outcome} summary={s.summary}{RST}")


async def cmd_voices(_args) -> None:
    from app.tts import ElevenLabsTTS

    tts = ElevenLabsTTS(settings)
    try:
        voices = await tts.list_voices()
    finally:
        await tts.aclose()
    rows = [{"voice_id": v["voice_id"], "name": v["name"], "category": v["category"],
             "accent": v["labels"].get("accent", ""), "gender": v["labels"].get("gender", "")} for v in voices]
    table(rows, ["voice_id", "name", "category", "gender", "accent"])
    print(f"\nDefault (ELEVENLABS_VOICE_ID): {settings.elevenlabs_voice_id}")


def cmd_set_voice(args) -> None:
    path = CAMPAIGN_DIR / args.name / "campaign.json"
    config = json.loads(path.read_text())
    config["voice_id"] = args.voice_id
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"{GRN}voice_id set to {args.voice_id or '(default)'}{RST} in {path}")


def _server(args) -> tuple[str, dict]:
    params = {"token": settings.dashboard_token} if settings.dashboard_token else {}
    return args.server.rstrip("/"), params


def cmd_http(args) -> None:
    base, params = _server(args)
    try:
        if args.cmd == "start":
            r = httpx.post(f"{base}/api/campaigns/{args.name}/start", params=params, timeout=30)
        elif args.cmd == "stop":
            r = httpx.post(f"{base}/api/campaigns/{args.name}/stop", params=params, timeout=30)
        else:
            r = httpx.get(f"{base}/api/campaigns/{args.name}", params=params, timeout=30)
    except httpx.ConnectError:
        sys.exit(f"Can't reach the server at {base}. Start it with: uvicorn app.main:app --port 8000")
    body = r.json()
    if r.status_code >= 400:
        sys.exit(f"{RED}{body.get('detail', body)}{RST}")
    if args.cmd == "start":
        print(f"{GRN}Started.{RST} {body['will_call']} contact(s) will be called. Watch: {base}/dashboard")
    elif args.cmd == "stop":
        print(body)
    else:
        print(f"{BOLD}{body['title']}{RST}  running={body['running']}  active calls={body['active_calls']}")
        table(body["contacts"], ["id", "name", "phone", "call_status", "outcome", "attempts", "reason"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("new", "validate", "start", "stop", "status"):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--server", default=os.getenv("SERVER_URL", "http://localhost:8000"))
    p = sub.add_parser("chat")
    p.add_argument("name")
    p.add_argument("--contact")
    sub.add_parser("voices")
    p = sub.add_parser("set-voice")
    p.add_argument("name")
    p.add_argument("voice_id")
    args = ap.parse_args()

    if args.cmd == "new":
        cmd_new(args)
    elif args.cmd == "validate":
        cmd_validate(args)
    elif args.cmd == "chat":
        asyncio.run(cmd_chat(args))
    elif args.cmd == "voices":
        asyncio.run(cmd_voices(args))
    elif args.cmd == "set-voice":
        cmd_set_voice(args)
    else:
        cmd_http(args)


if __name__ == "__main__":
    main()

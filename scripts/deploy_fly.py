#!/usr/bin/env python3
"""Deploy the voice agent to Fly.io from your Mac. Uses only the Python standard library.

    python3 scripts/deploy_fly.py            first time: app + volume + keys + deploy + Twilio webhook
    python3 scripts/deploy_fly.py --deploy   ship code or campaign changes (keys stay as they are)
    python3 scripts/deploy_fly.py --keys     enter or change keys, then deploy
    python3 scripts/deploy_fly.py --status   show the app URL, health, and dashboard link

Keys come from .env on this Mac (if you pasted them there) or are typed here (hidden). Each one is
checked against its provider, then sent to Fly as an encrypted secret. .env never leaves this Mac. (The dashboard password that this
script makes is kept in .fly-deploy.json and .dashboard-link.txt so you can open the dashboard.)
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".fly-deploy.json"
LINK = ROOT / ".dashboard-link.txt"
TEMPLATE = ROOT / "deploy" / "fly.toml.template"
PREFERRED_MODELS = ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1-nano", "gpt-4.1", "gpt-4o"]
DEFAULT_VOICE = "EXAVITQu4vr4xnSDxMaL"  # "Sarah"

B, G, Y, R, D, X = "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: str, title: str) -> None:
    say(f"\n{B}{n}  {title}{X}")


def ok(msg: str) -> None:
    say(f"   {G}✓{X} {msg}")


def warn(msg: str) -> None:
    say(f"   {Y}!{X} {msg}")


def fail(msg: str) -> None:
    say(f"   {R}✗{X} {msg}")
    sys.exit(1)


# ------------------------------------------------------------------ helpers
def fly_bin() -> str:
    for name in ("fly", "flyctl"):
        found = shutil.which(name)
        if found:
            return found
    home = Path.home() / ".fly" / "bin" / "flyctl"
    if home.exists():
        return str(home)
    fail("flyctl is not installed. Install it, then run this again:\n"
         "       brew install flyctl        (or)   curl -L https://fly.io/install.sh | sh")
    return ""


def fly(*args: str, input_text: str | None = None, capture: bool = True, check: bool = True):
    cmd = [fly_bin(), *args]
    say(f"   {D}$ fly {' '.join(args)}{X}")  # keys never appear in args; they go through stdin
    res = subprocess.run(cmd, input=input_text, text=True, capture_output=capture)
    if check and res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip() if capture else ""
        fail(f"fly {' '.join(args[:2])} failed. {detail[-600:]}")
    return res


def http(method: str, url: str, headers: dict | None = None, data: dict | None = None,
         basic: tuple | None = None, timeout: int = 20):
    """Return (status, parsed_json_or_text)."""
    as_json = method == "POST_JSON"
    if as_json:
        method = "POST"
        body = json.dumps(data).encode()
    else:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    if basic:
        token = base64.b64encode(f"{basic[0]}:{basic[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json" if as_json else "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode(errors="ignore")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(errors="ignore"), exc.code
    except Exception as exc:  # network problem
        return 0, str(exc)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    val = input(f"   {prompt}{shown}: ").strip()
    return val or default


def ask_secret(prompt: str, where: str) -> str:
    say(f"   {D}Get it here: {where}{X}")
    while True:
        val = getpass.getpass(f"   {prompt} (hidden, paste then Enter): ").strip()
        if val:
            return val
        warn("Empty. Paste the key, or press Ctrl+C to stop.")


def yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"   {prompt} [{d}]: ").strip().lower()
    return default if not val else val.startswith("y")


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2) + "\n")


# ------------------------------------------------------------------ steps
def setup_test_contact() -> None:
    """Offer to put your own phone (and email) in the loyalty campaign for the first real call."""
    path = ROOT / "campaigns" / "loyalty" / "contacts.csv"
    if not path.exists() or "+1XXXXXXXXXX" not in path.read_text():
        return
    step("0", "Your test contact (the first real call goes to you)")
    say(f"   {D}On a Twilio trial, this must be the number you verified when you signed up.{X}")
    phone = re.sub(r"[\s().-]", "", ask("Your mobile number, like +12105551234 (Enter to skip)"))
    if not phone:
        return
    if re.fullmatch(r"\d{10}", phone):
        phone = "+1" + phone
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        warn("That is not a full number with country code. Skipped; edit campaigns/loyalty/contacts.csv later.")
        return
    name = ask("Your first name", getpass.getuser().capitalize())
    email = ask("Your email for the follow-up test (Enter to skip)")
    text = path.read_text().replace("+1XXXXXXXXXX,Karthik,yes,you@example.com", f"{phone},{name},yes,{email}")
    path.write_text(text)
    ok("campaigns/loyalty/contacts.csv now has you as the first contact (consent=yes)")


def ensure_login() -> None:
    step("1", "Fly.io account")
    res = fly("auth", "whoami", check=False)
    if res.returncode != 0:
        say("   You are not logged in. A browser window opens. Sign in, then come back here.")
        fly("auth", "login", capture=False)
        res = fly("auth", "whoami")
    ok(f"logged in as {res.stdout.strip()}")


def ensure_app(state: dict) -> dict:
    step("2", "App and volume")
    if not state.get("app"):
        user = re.sub(r"[^a-z0-9]", "", getpass.getuser().lower())[:10] or "me"
        default = f"maya-{user}-{secrets.token_hex(2)}"
        name = ask("App name (becomes https://<name>.fly.dev)", default).lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", name):
            fail("Use lowercase letters, numbers and dashes.")
        region = ask("Region (dfw = Dallas, ord = Chicago, iad = Virginia, sjc = San Jose)", "dfw")
        state.update(app=name, region=region, url=f"https://{name}.fly.dev")
    app, region = state["app"], state["region"]
    (ROOT / "fly.toml").write_text(TEMPLATE.read_text().replace("{app}", app).replace("{region}", region))
    ok(f"fly.toml written for {app} in {region}")

    if fly("status", "-a", app, check=False).returncode != 0:
        say("   Creating the app (Fly may ask which organization to use).")
        fly("apps", "create", app, capture=False)
    ok(f"app {app} exists")

    vols = fly("volumes", "list", "-a", app, "--json", check=False)
    have = False
    try:
        have = any(v.get("name") == "data" for v in json.loads(vols.stdout or "[]"))
    except ValueError:
        pass
    if not have:
        fly("volumes", "create", "data", "--size", "1", "--region", region, "--yes", "-a", app)
    ok("volume 'data' (1 GB) holds results, transcripts and the do-not-call list")
    save_state(state)
    return state


def read_env_file() -> dict:
    """Keys you pasted into .env on this Mac (optional). Lines look like NAME=value."""
    path = ROOT / ".env"
    out: dict = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def collect_keys(state: dict) -> dict:
    """Take each key from .env (or ask for it), check it with the provider, and return the secrets for Fly."""
    out: dict = {}
    env = read_env_file()
    if env:
        ok(f"found .env with {sum(1 for v in env.values() if v)} values; I test those and ask only for the rest")

    def preset(name: str) -> str:
        return env.get(name, "")

    def get_key(name: str, label: str, where: str, test) -> tuple[str, object]:
        """Use the .env value if it passes the test; otherwise ask until one does."""
        key = preset(name)
        while True:
            if not key:
                key = ask_secret(label, where)
            status, body = test(key)
            if status == 200:
                return key, body
            warn(f"{label} was refused ({status}): {str(body)[:200]}")
            key = ""

    step("3a", "OpenAI (the brain)")
    key, body = get_key("LLM_API_KEY", "OpenAI API key", "https://platform.openai.com/api-keys",
                        lambda k: http("GET", "https://api.openai.com/v1/models", {"Authorization": f"Bearer {k}"}))
    ids = {m.get("id") for m in body.get("data", [])}
    model = preset("LLM_MODEL") if preset("LLM_MODEL") in ids else next((m for m in PREFERRED_MODELS if m in ids), None)
    if not model:
        warn("None of the fast chat models is on this key: " + ", ".join(PREFERRED_MODELS))
        model = ask("Model to use", "gpt-4.1-mini")
    status, chat = http("POST_JSON", "https://api.openai.com/v1/chat/completions", {"Authorization": f"Bearer {key}"},
                        data={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "Say ok"}]})
    if status != 200:
        warn(f"The key works, but a test reply failed ({status}): {str(chat)[:200]}")
        warn("Usually this means no credit: add a few dollars at https://platform.openai.com/settings/organization/billing")
    ok(f"key works; model {model}")
    out.update(LLM_PROVIDER="openai", LLM_API_KEY=key, LLM_MODEL=model, LLM_BASE_URL="")

    step("3b", "Deepgram (hearing)")
    key, _ = get_key("DEEPGRAM_API_KEY", "Deepgram API key",
                     "https://console.deepgram.com  →  API Keys  →  Create a New API Key",
                     lambda k: http("GET", "https://api.deepgram.com/v1/projects", {"Authorization": f"Token {k}"}))
    ok("key works")
    out["DEEPGRAM_API_KEY"] = key

    step("3c", "ElevenLabs (voice)")

    def eleven_test(k):
        st, b = http("GET", "https://api.elevenlabs.io/v1/voices", {"xi-api-key": k})
        if st == 401 and "permission" in str(b).lower():
            return 200, {"voices": [], "restricted": True}  # restricted key: speech works, listing doesn't
        return st, b
    key, body = get_key("ELEVENLABS_API_KEY", "ElevenLabs API key", "https://elevenlabs.io/app/settings/api-keys",
                        eleven_test)
    voices = body.get("voices", [])
    ids = {v["voice_id"] for v in voices}
    if body.get("restricted"):
        warn("This key can't list voices (restricted key). Speech still works; the dashboard voice list won't.")
    else:
        ok(f"key works; {len(voices)} voices on your account")
    voice = preset("ELEVENLABS_VOICE_ID")
    if not voice or (ids and voice not in ids):
        for v in voices[:12]:
            say(f"      {v['voice_id']}  {v.get('name')}")
        voice = ask("Default voice id", DEFAULT_VOICE if DEFAULT_VOICE in ids or not ids else voices[0]["voice_id"])
    ok(f"voice {voice}")
    out.update(ELEVENLABS_API_KEY=key, ELEVENLABS_VOICE_ID=voice)

    step("3d", "Twilio (phone)")
    sid, token = preset("TWILIO_ACCOUNT_SID"), preset("TWILIO_AUTH_TOKEN")
    while True:
        if not (sid and token):
            say(f"   {D}Get them here: https://console.twilio.com  (Account Info box on the home page){X}")
            sid = ask("Account SID (starts with AC)", sid)
            token = getpass.getpass("   Auth Token (hidden): ").strip()
        status, body = http("GET", f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", basic=(sid, token))
        if status == 200:
            kind = body.get("type", "?")
            ok(f"account works ({kind})")
            if kind == "Trial":
                warn("Trial account: you can call only verified numbers, callers hear a trial notice, and custom "
                     "SMS is blocked. Upgrade (smallest top-up) to call customers.")
            break
        warn(f"Twilio refused the SID/token ({status}): {str(body)[:200]}")
        token = ""
    status, body = http("GET", f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers.json",
                        basic=(sid, token))
    numbers = body.get("incoming_phone_numbers", []) if status == 200 else []
    if not numbers:
        fail("No phone number on this Twilio account. Buy one in the console (Phone Numbers → Buy), then run "
             "python3 scripts/deploy_fly.py --keys")
    pick = next((n for n in numbers if n["phone_number"] == preset("TWILIO_PHONE_NUMBER")), None)
    if pick is None:
        for i, n in enumerate(numbers, 1):
            say(f"      {i}. {n['phone_number']}  ({n.get('friendly_name')})")
        while True:
            try:
                pick = numbers[int(ask("Which number", "1")) - 1]
                break
            except (ValueError, IndexError):
                warn("Type the number from the list.")
    ok(f"using {pick['phone_number']}")
    out.update(TWILIO_ACCOUNT_SID=sid, TWILIO_AUTH_TOKEN=token, TWILIO_PHONE_NUMBER=pick["phone_number"])
    state["twilio_number"], state["twilio_number_sid"] = pick["phone_number"], pick["sid"]
    state["_twilio"] = (sid, token)  # used once below, never saved
    if preset("SMS_ENABLED"):
        out["SMS_ENABLED"] = "true" if preset("SMS_ENABLED").lower() == "true" else "false"
    else:
        say(f"   {D}US carriers block texts until Twilio approves your number (A2P 10DLC for local numbers, "
            f"Toll-Free Verification for toll-free; paid account, days). Until then Maya offers email only.{X}")
        out["SMS_ENABLED"] = "true" if yes("Has Twilio approved texting from this number?", default=False) else "false"
    ok(f"texting {'on' if out['SMS_ENABLED'] == 'true' else 'off (email only)'}")

    step("3e", "Email for follow-up links (optional)")
    provider = preset("EMAIL_PROVIDER").lower()
    choice = {"sendgrid": "1", "smtp": "2", "none": "3", "off": "3"}.get(provider, "")
    if provider == "sendgrid" and not preset("SENDGRID_API_KEY"):
        choice = ""
    if not choice:
        say("   Maya can email the offer link during the call. Choose: 1) Twilio SendGrid  2) Gmail/SMTP  3) skip")
        choice = ask("Email option", "1")
    if choice == "1":
        key = preset("SENDGRID_API_KEY")
        while True:
            if not key:
                key = ask_secret("SendGrid API key (Mail Send permission)", "https://app.sendgrid.com/settings/api_keys")
            status, body = http("GET", "https://api.sendgrid.com/v3/scopes", {"Authorization": f"Bearer {key}"})
            if status == 200 and "mail.send" in body.get("scopes", []):
                ok("key works")
                break
            warn("This key was refused or has no Mail Send permission." if status == 200
                 else f"SendGrid said {status}: {str(body)[:200]}")
            key = ""
        say(f"   {D}The From address must be a verified sender: SendGrid → Settings → Sender Authentication.{X}")
        out.update(EMAIL_PROVIDER="sendgrid", SENDGRID_API_KEY=key,
                   EMAIL_FROM=preset("EMAIL_FROM") or ask("From email address"),
                   EMAIL_FROM_NAME=preset("EMAIL_FROM_NAME") or ask("From name", "Maya"))
        ok(f"emails come from {out['EMAIL_FROM']}")
    elif choice == "2":
        say(f"   {D}Gmail: turn on 2-Step Verification, then make an app password at "
            f"https://myaccount.google.com/apppasswords{X}")
        user = preset("SMTP_USER") or ask("SMTP user (your Gmail address)")
        out.update(EMAIL_PROVIDER="smtp", SMTP_HOST=preset("SMTP_HOST") or ask("SMTP host", "smtp.gmail.com"),
                   SMTP_PORT=preset("SMTP_PORT") or ask("SMTP port", "587"), SMTP_USER=user,
                   SMTP_PASSWORD=preset("SMTP_PASSWORD") or getpass.getpass("   App password (hidden): ").strip(),
                   EMAIL_FROM=preset("EMAIL_FROM") or ask("From email address", user),
                   EMAIL_FROM_NAME=preset("EMAIL_FROM_NAME") or ask("From name", "Maya"))
    else:
        out.update(EMAIL_PROVIDER="", SENDGRID_API_KEY="")
        warn("Email skipped. Maya describes the perk and says the team will send the link; it's noted in results.")

    step("3f", "Supabase contacts and call history (optional)")
    sb_url, sb_key = preset("SUPABASE_URL").rstrip("/"), preset("SUPABASE_SERVICE_ROLE_KEY")
    if sb_url and sb_key:
        headers = {"apikey": sb_key, **({"Authorization": f"Bearer {sb_key}"} if sb_key.startswith("eyJ") else {})}
        status, body = http("GET", f"{sb_url}/rest/v1/call_conversations?select=id&limit=1", headers)
        if status == 200:
            ok("Supabase works: contacts from signup_requests, calls saved to call_conversations")
        else:
            warn(f"Supabase said {status}: {str(body)[:200]} (did you run supabase/schema.sql?)")
        out.update(SUPABASE_URL=sb_url, SUPABASE_SERVICE_ROLE_KEY=sb_key)
    else:
        say(f"   {D}Skipped: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env to turn it on.{X}")

    out["STREAM_SECRET"] = secrets.token_urlsafe(32)
    out["DASHBOARD_TOKEN"] = state.get("dashboard_token") or secrets.token_urlsafe(18)
    state["dashboard_token"] = out["DASHBOARD_TOKEN"]
    return out


def push_secrets(state: dict, values: dict) -> None:
    step("4", "Save keys on Fly (encrypted)")
    lines = "\n".join(f"{k}={v}" for k, v in values.items() if v != "") + "\n"
    fly("secrets", "import", "--stage", "-a", state["app"], input_text=lines)
    blanks = [k for k, v in values.items() if v == ""]
    if blanks:
        fly("secrets", "unset", "--stage", "-a", state["app"], *blanks, check=False)
    ok(f"{len(values)} secrets staged (they apply on the next deploy)")


def deploy(state: dict) -> None:
    step("5", "Build and deploy (2 to 5 minutes the first time)")
    fly("deploy", "-a", state["app"], "--remote-only", "--ha=false", "--yes", capture=False)
    url = state["url"]
    for _ in range(30):
        status, body = http("GET", f"{url}/health")
        if status == 200:
            if body.get("ok"):
                ok(f"{url} is up and healthy")
            else:
                warn(f"{url} is up, with problems: {body.get('problems')}")
            return
        time.sleep(3)
    warn(f"{url}/health did not answer yet. Check: fly logs -a {state['app']}")


def point_twilio(state: dict) -> None:
    creds = state.pop("_twilio", None)
    if not creds or not state.get("twilio_number_sid"):
        return
    step("6", "Connect your Twilio number")
    voice_url = f"{state['url']}/voice/incoming"
    if yes(f"Send calls to {state['twilio_number']} to Maya ({voice_url})?"):
        sid, token = creds
        status, body = http("POST", f"https://api.twilio.com/2010-04-01/Accounts/{sid}/IncomingPhoneNumbers/"
                                    f"{state['twilio_number_sid']}.json",
                            data={"VoiceUrl": voice_url, "VoiceMethod": "POST"}, basic=(sid, token))
        if status == 200:
            ok("inbound calls now reach Maya")
        else:
            warn(f"Twilio said {status}: {str(body)[:200]}. Set the Voice URL by hand in the console.")


def show_links(state: dict) -> None:
    url, token = state["url"], state.get("dashboard_token")
    say(f"\n{B}Done.{X}")
    if token:
        link = f"{url}/dashboard?token={token}"
        LINK.write_text(link + "\n")
        say(f"   Dashboard:  {link}")
        say(f"   {D}(also saved in .dashboard-link.txt; anyone with this link can see your calls){X}")
    if state.get("twilio_number"):
        say(f"   Phone:      call {state['twilio_number']} to talk to the inbound agent")
    say(f"   Logs:       fly logs -a {state['app']}")
    say("   Campaigns:  edit campaigns/<name>/ here, then run: python3 scripts/deploy_fly.py --deploy")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deploy", action="store_true", help="deploy code/campaign changes only")
    ap.add_argument("--keys", action="store_true", help="enter or change keys, then deploy")
    ap.add_argument("--status", action="store_true", help="show URL, health and dashboard link")
    args = ap.parse_args()
    os.chdir(ROOT)
    state = load_state()

    if args.status:
        if not state:
            fail("Not deployed yet. Run: python3 scripts/deploy_fly.py")
        status, body = http("GET", f"{state['url']}/health")
        say(f"{state['url']}  health={status} {body if status == 200 else ''}")
        show_links(state)
        return

    if not args.deploy:
        setup_test_contact()
    ensure_login()
    state = ensure_app(state)
    first_time = not state.get("keys_set")
    if args.keys or (first_time and not args.deploy):
        values = collect_keys(state)
        push_secrets(state, values)
        state["keys_set"] = True
        save_state({k: v for k, v in state.items() if not k.startswith("_")})
    elif first_time:
        warn("No keys yet. Run with --keys after this deploy.")
    deploy(state)
    point_twilio(state)
    save_state({k: v for k, v in state.items() if not k.startswith("_")})
    show_links(state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped.")

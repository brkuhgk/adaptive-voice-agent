# Adaptive Voice Agent (RowdyHacks prototype)

> Built for the [RowdyHacks](https://rowdyhacks.org/) hackathon competition.

> **New here?** Read [docs/GUIDE.md](docs/GUIDE.md). It explains the repo from first principles, with diagrams and
> step-by-step procedures (written in ASD-STE100 Simplified Technical English).

Two ways to use it:

- **Inbound:** call a phone number and talk to **Maya**, an AI scheduling assistant at a fictional clinic.
- **Outbound campaigns:** give Maya a list of phone numbers, your instructions, and your content. She calls each
  person who agreed to be called, has a real conversation, collects what you asked for, and writes the results to
  a CSV. You can pick the voice for each campaign, or for each contact.

Every reply is
generated live from the **agent context** (persona, goals, rules, knowledge) plus the **conversation state**
(what's known, what's missing, urgency, sentiment). There is no script and no decision tree. The only fixed
lines are the greeting (which says up front that Maya is an AI), the goodbye fallback, and the short
"one sec" fillers that cover tool lookups.

```
 caller ──► Twilio ──(8 kHz mu-law over WebSocket)──► FastAPI /media-stream
                                                        │
                    Deepgram nova-3 live STT ◄──────────┘   interim captions, turn end (endpointing + UtteranceEnd)
                                │ caller turn
                                ▼
             ┌──────────── Agent brain ─────────────────────────────────────────┐
             │  system prompt = agent context (scenarios/clinic.json)          │
             │                + live state snapshot + calendar                 │
             │  responder LLM ── streams text, calls tools ──► clinic schedule │
             │  state tracker LLM (runs in parallel) ──► intent, details,      │
             │                          sentiment, planner's next best action  │
             └──────────────────────────────┬───────────────────────────────────┘
                                            │ token stream → sentence chunks
                                            ▼
                ElevenLabs Flash TTS (ulaw_8000, streamed per sentence) ──► Twilio ──► caller hears it
```

What makes it feel like a conversation and not a phone tree:

| Behavior | How |
|---|---|
| Dynamic replies | Responder LLM sees context + live state each turn and decides what to say next |
| Grounded facts | Tools: `check_availability`, `book_appointment`, `lookup_appointment`, `cancel_appointment`, `flag_urgent`, `end_call` |
| Structured state | A parallel tracker call extracts intent, patient details, sentiment and a "next best action" (no added latency) |
| Low latency | Streaming LLM → sentence chunker → streaming TTS. Speech starts after the first sentence, not the whole reply. The greeting is pre-synthesized. |
| Barge-in | The caller talks over Maya → Twilio `clear` flushes her audio, and the LLM history is rewritten to show **only what the caller actually heard** (tracked with Twilio `mark`s) |
| Split turns | "I need to book…" (pause) "…for my son" → if Maya hasn't started speaking yet, both parts are merged into one turn |
| Silence | Re-prompts naturally after 9 s of silence, and hangs up politely after the third |
| Safety routing | Red-flag symptoms → tells the caller to call 911 (988 for self-harm) and ends the call. Never diagnoses. |
| Hang-up | `end_call` → waits until the goodbye has finished playing, then closes the stream |

A **live dashboard** (`/dashboard`) shows the transcript with interim captions, the agent's reply as it streams,
the barge-in cut-off point, the state panel, tool calls, booked appointments, and latency per turn. Put it on the
projector while judges call in.

---

## 1. Try it with no keys (2 minutes)

```bash
python3 -m venv .venv && source .venv/bin/activate      # Python 3.11+
pip install -r requirements.txt
python -m scripts.replay_demo                           # open http://localhost:8000/dashboard
```

`replay_demo` sends a scripted call through the real server and the Twilio media-stream protocol, with fake
STT/LLM/TTS. It includes a barge-in, two tool lookups, a booking and a hang-up. Use it to rehearse and to check
the dashboard. It costs nothing.

`python -m scripts.replay_demo --campaign` replays an **outbound** campaign call instead: Maya calls a sample
contact, answers a question from the campaign content, collects three fields, and writes the result.

Run the tests: `pytest` (48 tests: unit tests, provider wire-protocol tests against fake servers, and full
calls over a real WebSocket covering booking, barge-in, merging a split turn, silence handling, STT reconnects,
bad stream tokens, outbound campaigns, spoken and keypad opt-out, voicemail, and the dialer's rules).

## 2. Get keys (all have free tiers or credit)

| Service | Free option | What to copy into `.env` |
|---|---|---|
| **LLM: Microsoft Foundry** (recommended for demo day) | [Azure for Students](https://azure.microsoft.com/en-us/free/students): $100 credit, no credit card, school email | Endpoint + key, and a `gpt-4.1-mini` deployment |
| **LLM: GitHub Models** (free, good for dev) | Free with a GitHub account, same Foundry model catalog, rate-limited | Fine-grained token with **Models: read** |
| **Deepgram** | $200 starter credit on sign-up | API key |
| **ElevenLabs** | Free plan, 10k credits/month (roughly 10–20 min of speech). The $5 Starter plan is safer for demo day | API key (+ optional voice id) |
| **Twilio** | Trial credit + one free number | Account SID, auth token, phone number |

### Foundry setup (about 5 minutes)
1. Azure portal → create a **Microsoft Foundry** resource (or an Azure OpenAI resource).
2. In the Foundry portal → **Models + endpoints → Deploy model → `gpt-4.1-mini`** (Global Standard). Note the
   deployment name.
3. Copy the **endpoint** and **key** from the resource's *Keys and Endpoint* page.
4. In `.env`:
   ```
   LLM_PROVIDER=foundry
   LLM_BASE_URL=https://<your-resource>.openai.azure.com/openai/v1/
   LLM_API_KEY=<key>
   LLM_MODEL=<deployment name, e.g. gpt-4.1-mini>
   ```
   Use a fast, non-reasoning chat model. `gpt-4.1-mini` is supported until April 2027. Reasoning models
   (o-series, gpt-5-mini) add seconds of silence before each reply.

### GitHub Models instead (free)
```
LLM_PROVIDER=github
LLM_BASE_URL=            # leave empty
LLM_API_KEY=<github token with Models: read>
LLM_MODEL=openai/gpt-4.1-mini
```
Free limits are around 15 requests/min and 150/day on low-tier models, with 8k input tokens per request. Each
turn uses 2–3 calls, so this is fine for building but tight for a judging session. Set `STATE_TRACKER_ENABLED=false`
to halve the calls, or switch to Foundry for the demo.

## 3. Run it on a real phone

```bash
cp .env.example .env            # fill in keys
uvicorn app.main:app --port 8000
ngrok http 8000                 # or: cloudflared tunnel --url http://localhost:8000
```
1. Put the tunnel's https URL in `.env` as `PUBLIC_BASE_URL`, then restart uvicorn.
2. Run `python -m scripts.check_setup --configure-twilio`. It checks every key, measures LLM and TTS latency, and
   points your Twilio number's voice webhook at `PUBLIC_BASE_URL/voice/incoming`.
3. Open `http://localhost:8000/dashboard` and call your Twilio number.

On a Twilio **trial** account, callers first hear a short trial notice and press a key. Upgrading removes it.

**Outbound campaigns** (Maya calls a list). A campaign is one folder with four files you edit:

```
campaigns/<name>/
  campaign.json     agent name, org, voice, goal, opening line, fields to collect, calling hours
  instructions.md   how Maya should behave (your brief to her)
  content.md        the facts she can use
  contacts.csv      phone, name, consent (yes/no), optional timezone, voice_id, and any extra columns
```

```bash
python -m scripts.campaign new myevent        # copy the template
python -m scripts.campaign validate myevent   # see who will be called, and why not
python -m scripts.campaign chat myevent       # rehearse in text as a contact (nothing is saved)
python -m scripts.campaign start myevent      # or click "Start calls" on the dashboard
```

Results land in `campaigns/<name>/results.csv` (one row per contact, one column per collected field) and
`transcripts/`. Only rows with `consent=yes` are called, only inside calling hours, never twice after a final
outcome, and never if the number is in `campaigns/do_not_call.txt`. People can opt out by saying so or by
pressing 9. Full details: [docs/GUIDE.md](docs/GUIDE.md) sections 4–6.

**Calling people by hand (dashboard):** tick contacts and press **Call selected**, or press a row's **Call**
button. These calls go out even if the person was already called or ran out of attempts; consent, the
do-not-call list and calling hours still apply. **Add a number** at the top of the table (tick "They agreed to get
this call") to add someone without editing `contacts.csv`; dashboard-added people are saved in
`<DATA_DIR>/<name>/contacts_added.csv`, so a redeploy keeps them.

**Single test call** with the clinic agent: add the number to `OUTBOUND_ALLOWLIST`, then
`curl -X POST localhost:8000/api/call -H 'content-type: application/json' -d '{"to":"+12105550123"}'`.

**Text mode (backup demo):** the dashboard's input box (or `python -m scripts.simulate --state`) talks to the same
brain without a phone. It needs only an LLM key, so it still works if the venue Wi-Fi blocks the tunnel.

## 3b. Deploy to Fly.io (always on, no tunnel)

```bash
brew install flyctl                 # once
python3 scripts/deploy_fly.py       # app + volume + keys (checked, then stored as Fly secrets) + deploy
python3 scripts/deploy_fly.py --deploy   # after you edit campaign files
```

The script asks for each key with hidden input, tests it against the provider, stores it as an encrypted
Fly secret, deploys one always-on machine with a 1 GB volume (results and the do-not-call list), points your
Twilio number at `https://<app>.fly.dev/voice/incoming`, and prints the dashboard link (password included).

**Follow-up links during calls:** campaigns can define one offer per customer interest (`followups` in
`campaign.json`). Maya picks the offer that fits, asks permission, and sends it by email (Twilio SendGrid or
SMTP) or text (Twilio). US texting needs Twilio's Toll-Free Verification or A2P 10DLC approval first, so
`SMS_ENABLED` stays `false` until then. See `campaigns/loyalty/` and its `WORKSHEET.md`.

Email with Gmail (free, no sender approval): turn on 2-Step Verification, create an app password at
<https://myaccount.google.com/apppasswords>, then:

```bash
fly secrets set -a <app> EMAIL_PROVIDER=smtp SMTP_HOST=smtp.gmail.com SMTP_PORT=587 \
  SMTP_USER=you@gmail.com EMAIL_FROM=you@gmail.com EMAIL_FROM_NAME="Maya" SMTP_PASSWORD='xxxx xxxx xxxx xxxx'
```

## 4. Demo script for judges

Ask judges to make up any personal details. This is a demo line, not a real clinic.

1. **Happy path:** "I've had a fever since Tuesday, can I get seen?" Maya collects details one at a time, checks
   real availability, reads the details back, then books. Watch the state panel fill in.
2. **Interrupt her** while she's listing times: "actually, afternoons are better." She stops right away, and
   the dashboard strikes through the part you didn't hear.
3. **Change your mind / correct her:** "Wait, my birthday is the 12th, not the 2nd."
4. **Reschedule an existing appointment:** "This is Jordan Lee, born April 12 1998, I need to move my appointment."
   She verifies date of birth before sharing details.
5. **Ask a question:** "Where do I park?" / "Do you take new patients?"
6. **Safety:** "My dad has chest pain and his arm feels numb." Urgency turns red, she tells you to call 911 and
   hangs up.
7. **Go silent** for ~10 seconds. **Ask** "Are you a real person?" She says she's an  .
8. Point at the latency panel: time from end of speech to first audio.

## 5. Tuning

| Variable | Default | Effect |
|---|---|---|
| `DEEPGRAM_ENDPOINTING_MS` | 400 | Silence that ends a turn. Raise to 500–700 if Maya cuts people off while they read out numbers |
| `BARGE_IN_MIN_CHARS` | 4 | How much interim speech counts as an interruption ("uh-huh" is always ignored) |
| `SILENCE_TIMEOUT_S` | 9 | Seconds of silence before a re-prompt |
| `ELEVENLABS_VOICE_ID` | Sarah | Default voice. Campaigns and contacts can override it. `python -m scripts.campaign voices` lists voices |
| `LLM_TEMPERATURE` | 0.6 | Lower = more consistent, higher = more varied phrasing |
| `STATE_TRACKER_ENABLED` | true | Parallel extraction call; turn off on tight rate limits |

## 6. Changing the scenario

Everything Maya knows is in `scenarios/clinic.json`: persona, speaking style, goals, required fields, safety rules,
clinic knowledge, tool fillers and demo records. To change her personality or the clinic's policies, edit the JSON.
No code changes needed. For a different domain (restaurant, tech support), copy the file, set `SCENARIO=<name>`,
and swap the tools in `app/tools.py` and `app/scheduling.py` for that domain's actions.

## 7. Safety and ethics

- Maya says she is an AI in her first sentence, and admits it whenever asked.
- Outbound calls only reach people marked `consent=yes` (campaigns) or on `OUTBOUND_ALLOWLIST` (single test calls).
  Calls with an AI voice need the person's prior consent under US law (TCPA), and similar rules exist elsewhere.
- Campaign calls stay inside calling hours in the contact's time zone, and opt-outs (spoken or keypad 9) go to a
  permanent do-not-call list.
- Your campaign instructions can't override the fixed rules (AI disclosure, opt-out, wrong-person handling, no
  sensitive data).
- She never asks for SSNs, insurance IDs or payment details, and stops callers who start to share them.
- The clinic, providers and schedule are fictional, and everything is held in memory. This is **not** HIPAA
  compliant, so don't use it with real patient data.
- Each media stream is signed (HMAC of the CallSid) so random clients can't open one and spend your credits.
  Optional: `TWILIO_VALIDATE_SIGNATURE=true` checks Twilio webhook signatures, and `DASHBOARD_TOKEN` locks the
  dashboard and the text-mode API.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Call connects, then silence | Check `/health` and the uvicorn logs. Usually a bad ElevenLabs/LLM key or voice id. Run `check_setup` |
| "Application error" on the call | Twilio couldn't reach `/voice/incoming`. Check that the tunnel is up and the URL is in the number's config |
| Maya cuts you off mid-sentence | Raise `DEEPGRAM_ENDPOINTING_MS` |
| Maya talks over you / won't stop | Lower `BARGE_IN_MIN_CHARS`. Use a headset or handset instead of speakerphone |
| 403 on the webhook | `TWILIO_VALIDATE_SIGNATURE=true` but `PUBLIC_BASE_URL` doesn't match the URL Twilio calls |
| 429 errors from the LLM | GitHub Models rate limit. Set `STATE_TRACKER_ENABLED=false` or use Foundry |

## Layout

```
app/
  main.py           FastAPI: Twilio webhooks, /media-stream, campaigns API, dashboard, SSE events, text mode
  call_session.py   one live call: audio in/out, turn-taking, barge-in, silence, keypad opt-out, voicemail, hang-up
  agent.py          the brain: prompt + state → streaming LLM with tool loop; parallel state tracker
  profiles.py       call types: what differs between calls (context, tools, voice, results). Clinic profile here
  campaigns.py      outbound campaigns: files, rules, results.csv, do-not-call list, campaign profile
  dialer.py         walks a contact list and places calls through Twilio
  prompt.py         system prompt builder (static context first, live state last, so prompt caching works)
  state.py          ConversationState: fields, intent, urgency, derived phase
  tools.py          tool schemas + executor
  scheduling.py     in-memory clinic schedule (availability generated relative to today)
  stt.py / tts.py / llm.py   Deepgram, ElevenLabs, OpenAI-compatible (Foundry / GitHub / OpenAI) clients + fakes
  sentence.py       token stream → speakable chunks
  static/dashboard.html
scenarios/clinic.json   the inbound agent context
campaigns/demo/     a sample outbound campaign (placeholder numbers, so nobody is called by accident)
docs/GUIDE.md       first-principles guide (ASD-STE100) with diagrams and procedures
scripts/            campaign.py, replay_demo.py, simulate.py, check_setup.py
tests/              48 tests
```

## License

Released under the [MIT License](LICENSE). Built as part of [RowdyHacks](https://rowdyhacks.org/).

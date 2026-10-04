# Adaptive Voice Agent: user guide

This guide uses ASD-STE100 Simplified Technical English. Sentences are short. Procedures use
commands. Each flow with more than three steps has an ASCII diagram.

---

## 1. First principles

### 1.1 What a voice agent does

A phone call is sound in two directions. A computer cannot think about sound. It can think
about text. Thus a voice agent does four jobs again and again:

1. **Hear.** Change the sound of the person into text (speech-to-text, STT).
2. **Remember.** Keep a record of the call: what the person said, what the agent knows, and what the agent must still find out.
3. **Think.** Give the text, the record, and the instructions to a language model (LLM). The LLM writes the next reply.
4. **Speak.** Change the reply into sound (text-to-speech, TTS). Send the sound to the person.

```
                         ONE TURN OF THE CONVERSATION

  PERSON             TWILIO              SERVER (this repository)
 ┌────────┐ sound  ┌─────────┐ sound  ┌──────────────┐ text  ┌──────────────────────┐
 │ talks  │──────► │ phone   │──────► │ 1. HEAR      │─────► │ 2. REMEMBER          │
 └────────┘        │ network │        │ Deepgram STT │       │ state + agent context│
                   │         │        └──────────────┘       └──────────┬───────────┘
                   │         │                                          │ prompt
 ┌────────┐ sound  │         │ sound  ┌──────────────┐ text  ┌──────────▼───────────┐
 │ hears  │◄────── │         │◄────── │ 4. SPEAK     │◄───── │ 3. THINK             │
 └────────┘        └─────────┘        │ ElevenLabs   │       │ LLM (Foundry)        │
                                      └──────────────┘       └──────────────────────┘

                    The loop repeats until one side ends the call.
```

### 1.2 Why there is no script

A script is a fixed list of sentences. A script cannot answer a question that it does not
expect. This agent does not use a script. For each turn, the LLM gets three things:

- **Agent context.** Who the agent is, the goal, your instructions, and your facts.
- **Conversation state.** What the agent knows now and what it must still find out.
- **History.** The last part of the conversation.

The LLM writes a new reply from these three things. Only a small number of sentences are fixed:
the first sentence of the call, the voicemail message, and short "one moment" phrases.

### 1.3 The two types of call

| Type | Who starts the call | Who the agent is | Where the agent context is |
|---|---|---|---|
| Inbound | A person calls your Twilio number | A clinic front desk (sample) | `scenarios/clinic.json` |
| Outbound | The server calls each contact in a list | What you write | `campaigns/<name>/` |

This guide is mostly about outbound calls (campaigns).

---

## 2. The parts of the repository

| Path | Job |
|---|---|
| `app/main.py` | The web server. It receives the Twilio webhooks and the audio stream. It also serves the dashboard and the API. |
| `app/call_session.py` | One live call. It connects the four jobs. It also controls interruptions, silence, and the end of the call. |
| `app/agent.py` | The "think" job. It builds the prompt, gets the reply from the LLM, and runs the tools. |
| `app/profiles.py` | The interface for a type of call. The clinic profile is in this file. |
| `app/campaigns.py` | Campaign files, call rules, results, the do-not-call list, and the campaign profile. |
| `app/dialer.py` | The dialer. It goes through the contact list and starts the calls. |
| `app/stt.py` | Speech-to-text (Deepgram). |
| `app/tts.py` | Text-to-speech (ElevenLabs) and the list of voices. |
| `app/llm.py` | The LLM client (Microsoft Foundry, GitHub Models, or OpenAI). |
| `app/state.py` | The conversation state. |
| `app/static/dashboard.html` | The live dashboard. |
| `campaigns/<name>/` | One campaign: four files that you write. |
| `scripts/campaign.py` | Terminal commands for campaigns. |
| `scripts/replay_demo.py` | An offline test call. It needs no keys and no phone. |
| `scripts/check_setup.py` | A test of all keys and of the public URL. |
| `tests/` | Automatic tests (`pytest`). |

---

## 3. How one turn works

### 3.1 The normal turn

```
 person stops talking
        │
        ▼
 Deepgram finds the end of the turn (400 ms of silence) ──► text: "Medium, please."
        │
        ├─────────────────────────────────┐
        ▼                                 ▼
 RESPONDER LLM (streams words)      STATE TRACKER LLM (runs at the same time)
 prompt = context + state + history finds fields, mood, and the next best step
        │                                 │
        │ needs data or an action?        ▼
        ├── yes ──► tool, for example     state is updated
        │           end_call or opt_out   (dashboard and next prompt)
        │           result ──► LLM again
        ▼
 sentence splitter: "Got it, a medium." is ready before the full reply is ready
        │
        ▼
 ElevenLabs (voice of this call) ──► audio ──► Twilio ──► the person hears it
```

The first sentence goes to TTS immediately. Thus the person hears the agent before the LLM
completes the full reply.

### 3.2 When the person interrupts

```
 agent talks: "It's in the Student Union Ballroom, and check-in..."
        │
        ▼
 the person talks: "wait, where?"
        │
        ▼
 Deepgram sends the first words (interim text)
        │
        ▼
 server stops the reply and sends "clear" to Twilio ──► the audio stops at once
        │
        ▼
 server records only the sentences that the person heard (Twilio "marks")
        │
        ▼
 new turn with "wait, where?"  ──► the LLM knows what the person did not hear
```

Short sounds such as "uh-huh" do not stop the agent.

### 3.3 Other rules in a call

- **Silence.** After 9 seconds of silence, the agent asks if the person is there. After the third time, the agent ends the call.
- **Split turn.** If the person stops for a short time and then continues, the server joins the two parts into one turn.
- **End of call.** The agent ends the call only after the last sentence plays.
- **Maximum length.** At `max_call_seconds`, the agent ends the call politely.

---

## 4. Campaigns from first principles

### 4.1 What a campaign must know

To call a list of people, the agent must know five things:

| Question | File |
|---|---|
| Who do I call, and did each person agree? | `contacts.csv` |
| Why do I call, and what must I find out? | `campaign.json` (`goal`, `collect`) |
| How must I behave? | `instructions.md` |
| Which facts can I say? | `content.md` |
| When can I call, how many at a time, with which voice? | `campaign.json` |

A campaign is one folder with these four files:

```
campaigns/
├── do_not_call.txt          numbers that asked for no more calls (all campaigns)
└── demo/                    one campaign
    ├── campaign.json        settings
    ├── instructions.md      your instructions to the agent
    ├── content.md           facts that the agent can use
    ├── contacts.csv         the people to call
    ├── results.csv          made by the server: one row for each contact
    └── transcripts/         made by the server: one file for each call
```

### 4.2 How the prompt is built

The server builds a new prompt for each turn. The prompt has layers:

```
 ┌──────────────────────────────────────────────────┐ ◄── highest priority
 │ 1. Fixed rules        (code: app/campaigns.py)   │     AI disclosure, opt-out, wrong person,
 │                                                  │     no sensitive data, spoken style
 ├──────────────────────────────────────────────────┤
 │ 2. Goal               (campaign.json "goal")     │
 │ 3. Your instructions  (instructions.md)          │
 │ 4. Your facts         (content.md)               │
 │ 5. Contact details    (the row in contacts.csv)  │
 │ 6. Fields to collect  (campaign.json "collect")  │
 │ 7. Live state         (changes each turn)        │
 └──────────────────────────────────────────────────┘
```

Your instructions cannot change the fixed rules. For example, an instruction such as
"say that you are a human" has no effect. The agent always says that it is an AI.

### 4.3 The fixed rules

The agent always does these things. You do not write them.

- It says that it is an AI in the first sentence, and each time somebody asks.
- It makes sure that it talks to the correct person. If it is the wrong person, it does not give details. It ends the call with the outcome `wrong_person`.
- If the person asks for no more calls, it adds the number to `do_not_call.txt` and ends the call.
- The person can push the opt-out key (default `9`) at any time. The result is the same.
- If the time is bad for the person, it asks when to call again. The outcome is `callback_requested`.
- It does not ask for passwords, codes, Social Security numbers, or bank or card numbers.
- It only says facts from `content.md` and from the contact row.
- It gives `org_name` and `callback_number` if the person asks who calls.

### 4.4 File reference: `campaign.json`

| Key | Example | Description |
|---|---|---|
| `title` | `"Hack Night check-in"` | The name on the dashboard. |
| `agent_name` | `"Maya"` | The name that the agent uses. |
| `org_name` | `"Hack Night"` | The organization that the agent calls for. Required. |
| `callback_number` | `"(210) 555-0142"` | The number that the agent gives to people. |
| `voice_id` | `""` | The ElevenLabs voice. Empty = the default voice from `.env`. |
| `goal` | `"Confirm if {first_name} ..."` | The purpose of the call in one or two sentences. Required. |
| `opening_line` | `"Hi, this is Maya, an   ..."` | The first sentence. If it does not say "AI", the server adds an AI disclosure. |
| `closing_line` | `"Thanks, {first_name}. Bye!"` | Used if the LLM ends the call without a goodbye. |
| `voicemail_message` | `"Hi {first_name}, ..."` | Message for an answering machine. Empty = end the call with no message. |
| `collect` | see below | The fields that the agent must find out. They become columns in `results.csv`. |
| `outcomes` | `["completed", ...]` | The outcomes that the LLM can choose when it ends a call. |
| `calling_hours` | `{"start": "09:00", "end": "20:00", "timezone": "America/Chicago", "days": "mon-sun"}` | The dialer calls only in this time, in the time zone of the contact. |
| `max_concurrent_calls` | `1` | The number of calls at the same time. |
| `max_attempts` | `2` | The maximum number of calls to one contact. |
| `ring_timeout_seconds` | `25` | The time to ring before "no answer". |
| `answering_machine` | `"detect"` | `"detect"` = Twilio finds machines. `"off"` = no detection. |
| `opt_out_digit` | `"9"` | The key that removes the number. |
| `max_call_seconds` | `300` | The maximum length of a call. |
| `keyterms` | `["Hack Night"]` | Names that speech-to-text must hear correctly. |

The `collect` key:

```json
"collect": {
  "attending":     {"description": "Are they still coming? yes, no, or maybe", "required": true},
  "tshirt_size":   {"description": "T-shirt size: XS, S, M, L, XL, or XXL",     "required": true},
  "dietary_needs": {"description": "Food allergies or dietary needs, or none",  "required": false}
}
```

The agent asks for the required fields first. The description tells the agent what a good answer is.

### 4.5 File reference: `instructions.md`

Write this file as a short brief for a new team member.

- Tell the agent the order of the conversation.
- Tell the agent what to do when the answer is "no".
- Tell the agent which tone to use.
- Do not put facts in this file. Put facts in `content.md`.

The server removes text between `<!--` and `-->`. Use these markers for notes to yourself.

### 4.6 File reference: `content.md`

Put each fact that the agent can say in this file: dates, places, prices, rules, and answers to
usual questions. Use short lines. The agent says only these facts. If a fact is not in the
file, the agent says that a person will send the answer later.

Keep `instructions.md` and `content.md` together below approximately 12,000 characters. A
longer prompt makes each reply slower.

### 4.7 File reference: `contacts.csv`

| Column | Required | Description |
|---|---|---|
| `phone` | Yes | The number in E.164 format, for example `+12105550123`. The server adds `+1` to a 10-digit US number. |
| `name` | Yes | The full name. The agent uses the first word as the first name. |
| `consent` | Yes | `yes` only if this person agreed to get this call. Each other value = do not call. |
| `timezone` | No | For example `America/New_York`. Empty = the campaign time zone. |
| `voice_id` | No | A voice for this contact only. |
| `id` | No | A stable ID. Empty = the digits of the phone number. |
| other columns | No | Each other column (for example `team`) goes to the agent as a contact detail, and to placeholders. |

The agent never sees the `phone`, `consent`, `timezone`, `voice_id`, or `id` columns.

### 4.8 Placeholders

You can use placeholders in `goal`, `opening_line`, `closing_line`, `voicemail_message`,
`instructions.md`, and `content.md`:

| Placeholder | Value |
|---|---|
| `{first_name}` | The first word of `name` |
| `{name}` | The full name |
| `{agent_name}`, `{org_name}` | From `campaign.json` |
| `{callback_number}` | From `campaign.json` |
| `{team}` or each other column | The value in the contact row |

An unknown placeholder stays in the text without change.

### 4.9 Voices

The server selects the voice for each call in this order:

1. `voice_id` in the contact row.
2. `voice_id` in `campaign.json`.
3. `ELEVENLABS_VOICE_ID` in `.env`.

You can change the voice in three ways: on the dashboard, with a command, or in the file.
Section 6.9 gives the procedure.

### 4.9a Follow-up links (email and text)

During the call, the agent can send a link that matches the interest of the person. You write one
option for each interest in `campaign.json` under `followups`:

```json
"followups": {
  "channels": ["email", "sms"],
  "max_per_call": 2,
  "options": {
    "home_brewing": {
      "label": "Join + 15% off beans",
      "when": "They brew coffee at home or buy beans.",
      "link": "https://example.com/rewards/join?offer=beans",
      "sms": "Hi {first_name}, join free and get 15% off beans: {link}",
      "email_subject": "{first_name}, your rewards link",
      "email_body": "Hi {first_name},\n\nJoin here: {link}"
    }
  }
}
```

```
 person says what they like ──► agent selects the option ("when")
        │
        ▼
 agent describes the matching perk in one sentence
        │
        ▼
 agent asks: "Do you want the link by text or by email?"
        │
        ├── no ──► no message
        ▼ yes
 agent calls send_followup ──► server sends in the background ──► the call continues
        │
        ▼
 results.csv column "followups": home_brewing:email:sent
```

The rules of the server:

- The agent must get a clear yes first.
- A text goes only to the phone number in `contacts.csv`. It never goes to a number that somebody says.
- An email goes to the `email` column. If the column is empty, the agent asks for the address and spells it back.
- The agent never reads a link aloud.
- The agent offers only the channels that can send now.

> **CAUTION:** US carriers block texts from a Twilio number until Twilio approves it (Toll-Free
> Verification or A2P 10DLC, paid account, approximately 3 to 5 business days). Until approval,
> keep `SMS_ENABLED=false`. Then the agent offers email only.

### 4.10 What happens when the dialer runs

The dialer examines each contact from the top of `contacts.csv` to the bottom:

```
 contact row
      │
      ▼
 phone in E.164 format? ─────────────────── no ──► skip: "phone is not in +15551234567 format"
      │ yes
      ▼
 first time this phone is in the list? ──── no ──► skip: "duplicate phone number"
      │ yes
      ▼
 consent = yes? ─────────────────────────── no ──► skip: "no consent"
      │ yes
      ▼
 not in do_not_call.txt? ────────────────── no ──► skip: "on do-not-call list"
      │ yes
      ▼
 no final outcome yet? ──────────────────── no ──► skip: "done (completed)"
      │ yes
      ▼
 attempts < max_attempts? ───────────────── no ──► skip: "max attempts reached"
      │ yes
      ▼
 inside calling hours (contact time zone)? ─ no ──► skip: "outside calling hours"
      │ yes
      ▼
 wait for a free line (max_concurrent_calls) ──► CALL
```

The full path of one outbound call:

```
 YOU                 SERVER                           TWILIO                 CONTACT
  │                    │                                 │                       │
  │── start ─────────► │ dialer selects next contact     │                       │
  │                    │ makes the first sentence ready  │                       │
  │                    │── create call ────────────────► │── ring ─────────────► │
  │                    │                                 │◄── answer ─────────── │
  │                    │                                 │ machine detection     │
  │                    │◄── /voice/campaign ──────────── │                       │
  │                    │   person  → live conversation   │                       │
  │                    │   machine → voicemail or end    │                       │
  │                    │◄═══ /media-stream (audio) ════► │◄═══ audio ══════════► │
  │                    │   turns (section 3)             │                       │
  │                    │   end_call or opt_out           │                       │
  │                    │── close stream ───────────────► │── end call ─────────► │
  │                    │◄── /voice/status (completed) ── │                       │
  │◄── results.csv ─── │ write result and transcript     │                       │
```

### 4.11 Results

`results.csv` has one row for each contact that the dialer called. The server writes the row
again after each change.

| Column | Description |
|---|---|
| `call_status` | The last status from Twilio: `dialing`, `ringing`, `in-progress`, `completed`, `busy`, `no-answer`, `failed`, `canceled`. |
| `outcome` | The result of the conversation (see below). |
| `attempts` | The number of calls to this contact. |
| fields from `collect` | One column for each field, for example `tshirt_size`. |
| `summary` | One sentence from the agent: what happened and the open questions. |
| `callback_time` | Only for `callback_requested`. |
| `duration_s`, `call_sid`, `voice_id`, `updated_at`, `note` | Call details. |

The outcomes:

| Outcome | Set by | Call again? |
|---|---|---|
| `completed` | LLM | No |
| `not_interested` | LLM | No |
| `callback_requested` | LLM | No (a person must examine `callback_time`) |
| `wrong_person` | LLM | No |
| `opted_out` | LLM, or the opt-out key | Never. The number is in `do_not_call.txt`. |
| `hung_up` | Server: the person ended the call first | No |
| `voicemail_left` | Server | Yes, if attempts remain |
| `machine_no_message` | Server | Yes, if attempts remain |
| `no_conversation` | Server: the call connected, but nobody talked | Yes, if attempts remain |
| (empty) with `busy`, `no-answer`, `failed` | Twilio | Yes, if attempts remain |

```
 dialing ──► ringing ──┬──► busy ─────────┐
    │                  ├──► no-answer ────┤
    └──► failed ───────┼──────────────────┴──► no outcome ──► call again on a later start,
                       │                                       if attempts remain
                       ▼
                  in-progress ──► completed ──► the server sets the outcome
                                                      │
                     ┌────────────────────────────────┴──────────────────────┐
                     ▼                                                       ▼
        final outcome: completed, not_interested,          voicemail_left, machine_no_message,
        callback_requested, wrong_person, opted_out,       no_conversation
        hung_up ──► never call again                       ──► call again, if attempts remain
```

The dialer does not call again in the same run. Click **Start calls** again later to call the
contacts that remain.

---

## 5. Safety and the law

> **WARNING:** CALL ONLY PEOPLE WHO AGREED TO GET THIS CALL. In the USA, calls with an AI voice
> are "artificial voice" calls under the TCPA. They need the prior consent of the person. Other
> countries have similar laws. This guide is not legal advice.

The software helps you to follow these rules:

- The dialer calls only rows with `consent` = `yes`.
- The agent says that it is an AI in the first sentence.
- The agent gives the name of the organization and a callback number.
- The person can stop all calls with words or with one key.
- The dialer calls only inside `calling_hours`. US rules for telephone solicitation let you call only from 8 a.m. to 9 p.m., in the time zone of the person. The default is 9 a.m. to 8 p.m.
- The dialer never calls a number in `do_not_call.txt`.

> **CAUTION:** Do not put `contacts.csv`, `results.csv`, `transcripts/`, or `do_not_call.txt`
> in a public Git repository. These files contain phone numbers and personal data. The
> `.gitignore` file excludes the results, the transcripts, and the do-not-call list.

> **CAUTION:** Do not remove numbers from `do_not_call.txt`. A person on this list asked for no
> more calls.

---

## 6. Procedures

The full path from a new computer to a finished campaign:

```
 6.1 install ──► 6.2 offline test ──► 6.3 keys ──► 6.4 server + tunnel ──► 6.5 make campaign
                                                                                │
 6.10 read results ◄── 6.8 start ◄── 6.7 test call to yourself ◄── 6.6 text test┘
```

### 6.1 Install the software

1. Install Python 3.11 or a newer version.
2. Open a terminal in the repository folder.
3. Type `python3 -m venv .venv` and push Enter.
4. Type `source .venv/bin/activate` and push Enter. On Windows, type `.venv\Scripts\activate`.
5. Type `pip install -r requirements.txt` and push Enter.
6. Type `pytest` and push Enter.
7. Make sure that all tests pass.

### 6.2 Do an offline test

This test needs no keys, no phone, and no credit.

1. Type `python -m scripts.replay_demo --campaign` and push Enter.
2. Open `http://localhost:8000/dashboard` in a web browser.
3. Look at the call. The agent calls "Alex Garcia" and collects three fields.
4. Look at the **Outbound campaigns** panel. The outcome is `completed`.
5. Push Ctrl+C in the terminal to stop the server.

Note: `python -m scripts.replay_demo` without `--campaign` shows an inbound clinic call with an interruption.

### 6.3 Get the keys

1. Get an LLM key. Use Microsoft Foundry (Azure for Students gives credit) or GitHub Models (free, with limits). README section 2 gives the steps.
2. Make a Deepgram account. Copy the API key.
3. Make an ElevenLabs account. Copy the API key.
4. Make a Twilio account. Get one phone number. Copy the Account SID and the Auth Token.
5. Type `cp .env.example .env` and push Enter.
6. Open `.env`. Write each key in its line.
7. Write a long random text in `STREAM_SECRET`.
8. Save `.env`.

> **CAUTION:** Do not share `.env`. The keys in this file give access to your paid accounts.

Note: A Twilio trial account can call only the "Verified Caller IDs" in the Twilio console. Add your own number to this list first.

### 6.4 Start the server and the tunnel

Twilio must reach your computer from the internet. A tunnel gives your computer a public URL.

1. Type `uvicorn app.main:app --port 8000` and push Enter. Keep this terminal open.
2. Open a second terminal.
3. Type `ngrok http 8000` and push Enter. Keep this terminal open.
4. Copy the `https://` URL that ngrok shows.
5. Write this URL in `.env` as `PUBLIC_BASE_URL`. Do not add a `/` at the end.
6. Stop the server (Ctrl+C) and start it again (step 1).
7. Open a third terminal. Type `python -m scripts.check_setup --configure-twilio` and push Enter.
8. Make sure that each line has a green check mark.

Note: The free ngrok URL changes each time ngrok starts. Then do steps 4 to 8 again.

### 6.5 Make a campaign

1. Type `python -m scripts.campaign new myevent` and push Enter. The command copies the template to `campaigns/myevent/`.
2. Open `campaigns/myevent/campaign.json`. Change `title`, `org_name`, `callback_number`, `goal`, `opening_line`, `voicemail_message`, and `collect`.
3. Open `instructions.md`. Write your instructions to the agent (section 4.5).
4. Open `content.md`. Replace the sample facts with your facts (section 4.6).
5. Open `contacts.csv`. Add one row for each person (section 4.7).
6. Write `yes` in the `consent` column only for people who agreed.
7. Type `python -m scripts.campaign validate myevent` and push Enter.
8. Read each `error` and each `warning`. Correct the files.
9. Do step 7 again until there is no `error`.

> **WARNING:** Write `consent` = `yes` only if the person agreed to this call. Do not guess.

### 6.6 Test the campaign in text mode

Text mode uses the real LLM and your real files. It does not call anybody. It does not write
`results.csv` or `do_not_call.txt`.

1. Type `python -m scripts.campaign chat myevent` and push Enter.
2. Type your replies as the contact. Push Enter after each reply.
3. Try a "yes" path, a "no" path, a question that is not in `content.md`, and "please stop calling me".
4. Read the `collected=` line after each reply. Make sure that the agent fills each field.
5. If the agent says something wrong, change `instructions.md` or `content.md`. Then do step 1 again.

Note: To test as a specific contact, add `--contact <id>`. To test on the dashboard, select the campaign in the list next to the text box.

### 6.7 Do a test call to yourself

1. Put your own number in the first row of `contacts.csv`. Write `yes` in `consent`.
2. Put `max_concurrent_calls` = `1` in `campaign.json`.
3. Make sure that the other rows have `consent` = `no` for this test. Or make a separate test campaign.
4. Open `http://localhost:8000/dashboard`.
5. Click **Start calls** in the **Outbound campaigns** panel.
6. Answer your phone. Talk to the agent.
7. Look at the dashboard during the call. Examine the transcript, the state, and the tools.
8. After the call, examine the row in the table. Make sure that the outcome is correct.

### 6.8 Start the campaign

> **WARNING:** Before you start, make sure that each row with `consent` = `yes` is a person who agreed to this call.

1. Do procedure 6.5 step 7. Make sure that the "would be called now" number is correct.
2. Click **Start calls** on the dashboard. Or type `python -m scripts.campaign start myevent`.
3. Look at the **Outbound campaigns** panel. Each row shows the call status and the outcome.
4. To stop, click **Stop**. Or type `python -m scripts.campaign stop myevent`.

Note: **Stop** prevents new calls. Calls that are in progress continue until they end.

Note: The server reads the campaign files when you click **Start calls**. If you change a file during a run, the change has effect on the next run.

### 6.9 Change the voice

Use one of these three methods.

On the dashboard:

1. Select the campaign in the **Outbound campaigns** panel.
2. Select a voice in the **Voice** list.
3. Click **▶ Preview** to hear a sample.
4. Click **Use this voice**. The server writes the voice to `campaign.json`.

In the terminal:

1. Type `python -m scripts.campaign voices` and push Enter. Find the voice ID in the list.
2. Type `python -m scripts.campaign set-voice myevent <voice_id>` and push Enter.

For one contact only:

1. Open `contacts.csv`.
2. Write the voice ID in the `voice_id` column of the row.

Note: The dashboard does not let you change the voice while the campaign runs. Stop the campaign first. A change in the file during a run has effect on the next run.

### 6.10 Read the results

1. Open `campaigns/myevent/results.csv` in a spreadsheet program.
2. Filter the `outcome` column.
3. For `callback_requested`, read `callback_time`. Call the person back.
4. For a question in `summary`, send the answer to the person.
5. To read a full conversation, open the file in `campaigns/myevent/transcripts/`.

### 6.11 Call the other contacts again

1. Wait some hours, inside the calling hours.
2. Click **Start calls** again.
3. The dialer calls only contacts with no final outcome and with attempts that remain.

---

### 6.12 Deploy to Fly.io (a public server that is always on)

Twilio must reach the server from the internet at all times. A cloud server does this without a
tunnel. The script runs on your Mac. It sends your keys directly to Fly as encrypted secrets.

```
 python3 scripts/deploy_fly.py
        │
        ├── 0  your phone and email ──► first row of campaigns/loyalty/contacts.csv
        ├── 1  Fly login (browser)
        ├── 2  app + 1 GB volume (results, transcripts, do-not-call list)
        ├── 3  keys: OpenAI, Deepgram, ElevenLabs, Twilio, email ──► each key is tested
        ├── 4  keys ──► Fly secrets (encrypted)
        ├── 5  build + deploy ──► https://<app>.fly.dev/health
        └── 6  Twilio number ──► https://<app>.fly.dev/voice/incoming
```

1. Install flyctl: type `brew install flyctl` and push Enter.
2. Type `cd ~/Developer/adaptive-voice-agent` and push Enter.
3. Type `python3 scripts/deploy_fly.py` and push Enter.
4. Answer each question. Paste each key when the script asks for it.
5. Open the dashboard link that the script shows at the end.
6. To send changes to campaign files, type `python3 scripts/deploy_fly.py --deploy`.
7. To change a key, type `python3 scripts/deploy_fly.py --keys`.

Note: The dashboard link contains the dashboard password. Do not share it.

Note: A public server locks the dashboard if `DASHBOARD_TOKEN` is empty. The script sets it.

## 7. Problems and corrections

| Problem | Cause | Correction |
|---|---|---|
| "0 of N contacts would be called" | Each row has a skip reason | Read the `reason` column in `validate`. |
| "outside calling hours" | Local time of the contact is outside `calling_hours` | Wait, or change `calling_hours`. |
| "Set TWILIO_... in .env" | A Twilio key or `PUBLIC_BASE_URL` is not in `.env` | Do procedure 6.3 and 6.4. |
| The phone rings, then silence | The server cannot speak | Do `check_setup`. Examine the ElevenLabs key and the voice ID. |
| "Application error" on the phone | Twilio cannot reach the server | Make sure that ngrok runs and `PUBLIC_BASE_URL` is correct. |
| The agent talks to the voicemail greeting | `answering_machine` is `"off"` | Set `"detect"`. |
| The agent stops while you read a number | The silence time is too short | Set `DEEPGRAM_ENDPOINTING_MS` to 500 to 700 in `.env`. |
| The agent says a fact that is not correct | The fact is not in `content.md`, or it is not clear | Write the fact clearly in `content.md`. Do procedure 6.6. |
| A person calls back the Twilio number and hears the clinic agent | Inbound calls use the clinic profile | Set `callback_number` to a number that a person answers. |
| The voice list is empty on the dashboard | The ElevenLabs key is not in `.env` | Write `ELEVENLABS_API_KEY` in `.env`. |

---

## 8. Reference

### 8.1 Commands

| Command | Action |
|---|---|
| `uvicorn app.main:app --port 8000` | Start the server. |
| `python -m scripts.check_setup [--voices] [--configure-twilio]` | Test the keys and the public URL. |
| `python -m scripts.replay_demo [--campaign]` | Do an offline test call. |
| `python -m scripts.simulate` | Talk to the clinic agent in text. |
| `python -m scripts.campaign new <name>` | Make a campaign from the template. |
| `python -m scripts.campaign validate <name>` | Examine the files. Show who the dialer will call. |
| `python -m scripts.campaign chat <name> [--contact ID]` | Test the campaign in text. Nothing is saved. |
| `python -m scripts.campaign voices` | Show the ElevenLabs voices. |
| `python -m scripts.campaign set-voice <name> <voice_id>` | Set the campaign voice. |
| `python -m scripts.campaign start / status / stop <name>` | Control a campaign. The server must run. |

### 8.2 Web addresses on the server

| Address | Used by | Job |
|---|---|---|
| `/dashboard` | You | Live view and campaign controls. |
| `/voice/incoming` | Twilio | An inbound call starts. |
| `/voice/campaign` | Twilio | An outbound call is answered. |
| `/voice/status` | Twilio | Twilio sends the call status. |
| `/media-stream` | Twilio | The audio of the call (WebSocket). |
| `/api/campaigns`, `/api/campaigns/<name>` | Dashboard | Campaign list and details. |
| `/api/campaigns/<name>/start`, `/stop` | Dashboard, CLI | Start or stop the dialer. |
| `/api/voices` | Dashboard | The list of voices. |
| `/api/sim/message` | Dashboard | Text mode. |
| `/health` | You | The configuration problems. |

### 8.3 Settings in `.env`

README section 5 and `.env.example` give each setting. The settings that campaigns use most:
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `PUBLIC_BASE_URL`,
`ELEVENLABS_VOICE_ID`, `STREAM_SECRET`, and `DASHBOARD_TOKEN`.

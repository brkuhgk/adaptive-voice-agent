# Campaign worksheet: write a call campaign from first principles

This worksheet uses ASD-STE100 Simplified Technical English. Answer each question with short
facts. Each answer goes into one file. The sample answers are for a fictional coffee shop.
Replace them with your facts.

## Why first principles

A good call needs three things. If one is missing, do not make the call.

```
 ┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐
 │ 1. PERMISSION        │   │ 2. VALUE             │   │ 3. ONE NEXT STEP     │
 │ The person agreed    │ + │ The offer helps this │ + │ The person knows     │
 │ to get this call.    │   │ person, now.         │   │ exactly what to do.  │
 └──────────────────────┘   └──────────────────────┘   └──────────────────────┘
                                       │
                                       ▼
                   a short call that the person is glad to get
```

The agent does not use a script. It uses your facts and your rules to make each reply. Thus the
quality of the call depends on the quality of your answers.

## How your answers become the campaign

```
 Questions 1-2  ──► campaign.json  (org_name, agent_name, callback_number, opening_line)
 Question  3    ──► contacts.csv   (who, consent, email)
 Questions 4-5  ──► content.md     (the offer and the facts)
 Question  6    ──► campaign.json  (followups: one link and message for each interest)
 Question  7    ──► campaign.json  (collect: what the agent must find out)
 Questions 8-9  ──► instructions.md (the order of the call, tone, and limits)
 Question  10   ──► campaign.json  (calling_hours, max_attempts)
```

## The call, step by step

```
 confirm the person ──► say why you call ──► learn the interest ──► explain the offer
         │                                                                │
   wrong person?                                                          ▼
   end politely                       send the link ◄── yes ── ask: join? text or email?
                                            │                             │
                                            ▼                             no
                                 say what you sent ──► goodbye ◄──────────┘
```

---

## Questions

### 1. Who are you?

- Company name: **  starbucks Coffee Co.**
- What you sell, in one sentence: **Coffee, espresso drinks, pastries, and beans.**
- A phone number that a person answers: **4473474251**
- Website: **https://hackprank.com/**

### 2. Who is the agent?

- Agent name: **Maya**
- Tone, in three words: **warm, relaxed, brief**
- First sentence (it must say "AI"): **Hi, this is Maya, calling from   starbucks Coffee Co. Am I speaking with {first_name}?**

### 3. Who do you call, and how did they agree?

- Which customers: **Customers who bought in the last 90 days.**
- How they gave consent (written): **Checkbox at sign-up: "You may call or text me about offers, including with automated or AI calls."**
- Columns you have: **phone, name, email, favorite item**

> **WARNING:** Write `consent` = `yes` only for people who gave this written consent.

### 4. What is the offer?

- Name: **  starbucks Rewards**
- One sentence: **A free loyalty program for regular customers.**
- Price: **Free.**
- The three best benefits:
  1. **1 star per dollar; 100 stars = a free apple watch.**
  2. **A free birthday drink.**
  3. **Double stars on Tuesdays.**
- How to join: **With the link we send, or at the counter.**

### 5. What will people ask?

Write each question and its answer. The agent can say only these facts.

| Question | Answer |
|---|---|
| Does it cost money? | No, it is free. |
| Do stars expire? | Not while you visit at least once every 6 months. |
| Where is the shop? | 410 Market Street, San Antonio. |
| When are you open? | 6:30 a.m. to 7 p.m. every day. |

### 6. Which interests do your customers have? What do you send for each one?

Each interest gets one perk, one link, and one short message. The agent selects the interest from
what the person says.

| Key | When the agent selects it | Perk | Link |
|---|---|---|---|
| `home_brewing` | They brew at home or buy beans | 15% off beans | https://hackprank.com/ |
| `daily_coffee` | They come often, on the way to work | 6th drink free | https://hackprank.com/ |
| `food` | They talk about pastries or food | free pastry | https://hackprank.com/ |
| `events` | They like tastings or classes | early access | https://hackprank.com/ |

Keep each text message below 160 characters. Put `{link}` where the link goes.

### 7. What must the agent find out?

| Field | Required | Description |
|---|---|---|
| `joins_program` | Yes | yes, no, or maybe |
| `interest` | Yes | home_brewing, daily_coffee, food, or events |
| `favorite_drink` | No | their usual drink |
| `feedback` | No | any comment about the shop |

Each field becomes a column in `results.csv`.

### 8. What is the order of the call?

1. Confirm the person. Thank them for being a customer.
2. Say why you call, in one sentence.
3. Ask one easy question to learn the interest.
4. Explain the program in two sentences. Mention the one perk that matches.
5. Ask if they want to join. Ask: text or email?
6. Send the link. Say what you sent.

### 9. What must the agent never do?

- **Never say that the program costs money.**
- **Never promise a discount that is not in content.md.**
- **If there is a complaint: apologize, write it in the summary, and say that a manager will call.**

The fixed rules (AI disclosure, opt-out, wrong person, no sensitive data) apply always. You do not
write them.

### 10. When can the agent call?

- Hours: **09:00 to 20:00** (US law lets you call only from 8 a.m. to 9 p.m., in the time zone of the person)
- Days: **mon-sun**
- Maximum calls to one person: **2**

---

## When you finish

1. Send your answers (or edit this file).
2. The answers go into the four files.
3. Type `python -m scripts.campaign validate loyalty`. Correct each `error`.
4. Type `python -m scripts.campaign chat loyalty`. Test the call in text.
5. Type `python3 scripts/deploy_fly.py --deploy`. Then click **Start calls** on the dashboard.

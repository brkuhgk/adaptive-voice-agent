# Supabase: contacts and call history

The agent uses two tables in Supabase. Both are optional. Without Supabase keys, everything works from the
CSV files as before.

| Table | Who writes it | What the agent does with it |
|---|---|---|
| `signup_requests` | the sign-up site | Reads it. Each row becomes a campaign contact (shown with a **supabase** tag on the dashboard). |
| `call_conversations` | the agent | Writes one row per finished call: outcome, collected fields, follow-ups, full transcript. |
| `feedback_reports` | the sign-up site | Not used by the agent. |

The schema is in [`supabase/schema.sql`](../supabase/schema.sql). It is already applied to the project.

## 1. Turn it on

1. Supabase → **Project Settings → API Keys**. Copy the **service_role** key (or a **secret** `sb_secret_...` key).
   Keep it on the server only. It bypasses row-level security.
2. Add to `.env`:
   ```
   SUPABASE_URL=https://kjwgodmsdclkljukryft.supabase.co
   SUPABASE_SERVICE_ROLE_KEY=<the key>
   ```
3. Cloud: run `python -m scripts.deploy_fly`. Step 3f tests the key and saves it as a Fly secret.
4. Check: `/health` shows `"supabase": true`.

## 2. Contacts from `signup_requests`

A campaign uses Supabase contacts when its `campaign.json` has:

```json
"supabase_contacts": {"table": "signup_requests", "skip_status": ["revoked"]}
```

(`"supabase_contacts": true` uses those defaults.) The `loyalty` campaign has it on.

How a row becomes a contact:

| Column | Use |
|---|---|
| `phone` | Number to call. `(512) 555-0111` becomes `+15125550111`. |
| `name` | Name Maya uses. |
| `consent`, `status` | Callable only if `consent = true` and `status` is not in `skip_status`. Others show "no consent". |
| `email` | Used for follow-up links. Maya never reads it out. |
| `favorite_item`, `comments`, any new column | Given to Maya as "About the person you called". |
| `id` | Saved as `signup_request_id` on each call. Never shown to Maya. |

Rules:

- `contacts.csv` and dashboard-added people still work. If a phone is in both, the CSV row is kept and is
  linked to the sign-up.
- The list is cached for 10 seconds. New sign-ups appear on the next dashboard refresh.
- If Supabase is down, the dashboard shows a warning and uses the CSV contacts.
- If a person opts out on a call, they go on the do-not-call list (as before), **and** their sign-up gets
  `status = 'revoked'`.
- Results still go to `results.csv` and `transcripts/*.json`. Supabase is an added copy.

## 3. `call_conversations` (one row per call)

| Column | Meaning |
|---|---|
| `call_sid` | Twilio CallSid. Unique, so a re-save updates the same row. |
| `kind` | `campaign` (outbound) or `clinic` (inbound). |
| `campaign`, `contact_id`, `contact_name`, `phone` | Who was called (or who called in). |
| `signup_request_id` | Link to `signup_requests.id` (null for CSV-only contacts and inbound calls). |
| `mode` | `live` or `voicemail`. |
| `outcome` | Campaign: `completed`, `not_interested`, `callback_requested`, `wrong_person`, `opted_out`, `hung_up`, `no_conversation`, `voicemail_left`. Clinic: `booked`, `cancelled`, `emergency`, `completed`, `hung_up`, `no_conversation`. |
| `summary`, `callback_time` | Maya's one-sentence summary; when to call back. |
| `fields` | JSON of what was collected, e.g. `{"joins_program": "yes", "interest": "daily_coffee"}`. |
| `followups` | JSON list of links sent: `[{"option": "...", "channel": "email", "status": "sent"}]`. |
| `transcript` | JSON list: `[{"role": "agent"|"user", "text": "...", "t": 3.2}, ...]` (`t` = seconds into the call). |
| `state` | Final conversation state: intent, sentiment, appointment, labels. |
| `turns`, `duration_s`, `voice_id`, `started_at`, `ended_at` | Call stats. |

Typed tests on the dashboard (text mode) are **not** saved.

## 4. Reading the data later

### SQL editor (Supabase → SQL Editor)

```sql
-- Latest calls
select ended_at, contact_name, outcome, summary
from call_conversations order by ended_at desc limit 50;

-- Every sign-up with its most recent call (null = never called)
select s.name, s.phone, s.status, c.outcome, c.summary, c.ended_at
from signup_requests s
left join lateral (
  select * from call_conversations c
  where c.signup_request_id = s.id order by ended_at desc limit 1
) c on true
order by s.consented_at;

-- Who wants to join, and what they like
select contact_name, fields->>'interest' as interest, fields->>'favorite_drink' as drink
from call_conversations
where campaign = 'loyalty' and fields->>'joins_program' = 'yes';

-- One call's transcript, line by line
select t->>'role' as who, t->>'text' as said, (t->>'t')::float as at_s
from call_conversations, jsonb_array_elements(transcript) t
where call_sid = 'CA...';

-- Outcome counts per campaign
select campaign, outcome, count(*) from call_conversations group by 1, 2 order by 1, 3 desc;
```

### From code (server side, service_role key)

Python (`pip install supabase`):

```python
from supabase import create_client
sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
calls = (sb.table("call_conversations").select("contact_name,outcome,summary,transcript")
         .eq("campaign", "loyalty").order("ended_at", desc=True).limit(20).execute().data)
history = sb.table("call_conversations").select("*").eq("signup_request_id", signup_id).execute().data
```

JavaScript (Next.js server route / edge function, `@supabase/supabase-js`):

```js
const { data } = await supabase
  .from("call_conversations")
  .select("ended_at, outcome, summary, fields")
  .eq("signup_request_id", signupId)
  .order("ended_at", { ascending: false });
```

Plain HTTP:

```bash
curl "$SUPABASE_URL/rest/v1/call_conversations?select=contact_name,outcome,summary&order=ended_at.desc&limit=10" \
  -H "apikey: $SUPABASE_SERVICE_ROLE_KEY"
```

### Showing calls in a browser app

`call_conversations` has row-level security on and **no policies**, so the anon key cannot read it. That is on
purpose: transcripts are personal data. To show a person their own calls, read the table in a server route with
the service key. Or add a narrow policy for logged-in users, for example:

```sql
create policy "read own calls" on call_conversations for select to authenticated
using (phone = '+' || (auth.jwt() ->> 'phone'));  -- Supabase phone auth stores the number without '+'
```

## 5. Changing the schema

Add columns in `supabase/schema.sql` and run them in the SQL editor (`alter table ... add column ...`).
New columns in `signup_requests` need no code change: they reach Maya as contact details. To keep a column
away from Maya, add its name to `RESERVED_COLUMNS` in `app/campaigns.py`.

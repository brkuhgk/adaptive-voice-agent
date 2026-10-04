-- Tables the voice agent uses in Supabase. Run this once in the Supabase SQL editor.
--
--   signup_requests     (already exists, written by the sign-up site) -> campaign contacts
--   call_conversations  (created here) -> one row per call: outcome, collected fields, full transcript
--
-- The server talks to Supabase with the service_role key, which bypasses RLS. RLS is on with no
-- policies, so the anon key (used by browsers) can never read transcripts.

create table if not exists public.call_conversations (
  id                uuid primary key default gen_random_uuid(),
  call_sid          text not null unique,          -- Twilio CallSid (or text-... for typed tests)
  kind              text not null,                 -- 'campaign' (outbound) | 'clinic' (inbound)
  campaign          text,                          -- campaigns/<name>, null for clinic calls
  contact_id        text,                          -- the contact id the dashboard shows
  signup_request_id uuid references public.signup_requests (id) on delete set null,
  contact_name      text,
  phone             text,                          -- E.164: the number called, or the caller
  mode              text,                          -- live | voicemail
  outcome           text,                          -- completed, not_interested, opted_out, hung_up, booked...
  summary           text,
  callback_time     text,
  fields            jsonb not null default '{}'::jsonb,  -- what the agent collected
  followups         jsonb not null default '[]'::jsonb,  -- links sent by text/email during the call
  transcript        jsonb not null default '[]'::jsonb,  -- [{role, text, t, ...}]
  state             jsonb,                               -- final conversation state (intent, sentiment...)
  turns             integer,
  duration_s        integer,
  voice_id          text,
  started_at        timestamptz,
  ended_at          timestamptz not null default now()
);

create index if not exists call_conversations_signup_idx on public.call_conversations (signup_request_id);
create index if not exists call_conversations_campaign_idx on public.call_conversations (campaign, ended_at desc);
create index if not exists call_conversations_phone_idx on public.call_conversations (phone);

alter table public.call_conversations enable row level security;

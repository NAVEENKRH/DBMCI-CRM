# DBMCI Sales CRM

A lightweight CRM for the sales team: solves duplicate leads across sources
and missed/late follow-ups, using the Google Sheet as raw lead intake and
the CRM as the actual working interface.

## Features

- **Live sync from the Google Sheet** — click "Sync Now" and it pulls
  straight from the sheet (Lead Type, Date, Name, Numbers, Responsible
  columns), no export/upload needed. Safe to click repeatedly: only rows
  added since the last sync are processed.
- **Duplicate detection, nothing hidden** — mobile number is the only
  primary key. If a number already exists, the new lead is still added as
  its own row — never merged or dropped — and auto-tagged "Duplicate" with
  a link back to the original owner.
- **Lead Stage** (starts blank): DNP, Interested, Not Interested, Closed,
  Others, Duplicate, Callback.
- **Follow-up date + time, with exact-time reminders** — a date alone is
  just saved. Add a time too, and the CRM pops an in-app browser
  notification + banner the moment that time arrives (while a tab is open).
- **Round-robin assignment** — Naveen → Satish → Shiva → repeat, continuing
  from wherever it last stopped. Pulled from the sheet's "Responsible"
  column when present (handles "Sathish"/"Sathesh" spelling variants as
  aliases for "Satish"), otherwise auto-assigned.
- **Ownership-based permissions** — Naveen and Priyanka have full access
  (view + edit everything). Satish and Shiva can view all leads but can
  only edit ones assigned to them — enforced server-side, not just hidden
  in the UI.
- **Remarks** — an append-only conversation history per lead, never
  overwritten.
- **Manual CSV import** as a fallback for one-off files.

## Running it

```bash
cd sales-crm
python3 -m pip install --user -r requirements.txt
python3 app.py
```

The database is a single `crm.db` SQLite file in this folder.

### Access from other laptops on the same WiFi

The app binds to your machine's network address, not just localhost. Find
your laptop's LAN IP (`ipconfig getifaddr en0` on Mac) and share
`http://<that-ip>:5050` — teammates on the same WiFi open it directly, no
install needed on their end. If macOS prompts "Allow incoming connections
for python3?", click Allow. This only works while your laptop is on, awake,
and running the server.

## First-time / ongoing setup

1. **Agents** page — Naveen, Satish, Shiva (agents) and Priyanka (manager,
   full access) are pre-seeded. Add more people here if needed; a "Grant
   full access" toggle is available per agent.
2. **Sync Sheet** page — click "Sync Now" whenever new leads are added to
   the Google Sheet. The sheet must stay shared as "Anyone with the link —
   Viewer" for this to work.
3. Each person picks their name under **Acting as** (top right) — this
   both attributes remarks and enforces what they can edit.

## Design decisions worth knowing

- No login/password — "Acting as" is a lightweight, no-auth selector by
  design for a small internal tool. Edit permissions are still enforced
  server-side based on who's selected, so this isn't just cosmetic.
- Reminders are in-CRM only (no email/SMS/WhatsApp/calendar), and only fire
  while a browser tab is open on the CRM — this is an explicit scope
  decision, not a limitation to fix later.
- Duplicate matching is phone-number-only (last 10 digits, so formatting
  differences don't cause misses) — no name or email matching, matching how
  the team already treats mobile number as the unique key.
- The main leads table intentionally shows only 7 fields (Lead Type, Date,
  Name, Numbers, Responsible, Lead Stage, Follow-up) — no extra columns.

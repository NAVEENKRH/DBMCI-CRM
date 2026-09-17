import os
from datetime import datetime

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")

# "" (blank) = not called yet. Must stay first so the dropdown/column starts empty.
# This is the real production stage vocabulary (from the actual sheet/dropdown),
# not an invented simplification.
STAGES = [
    "",
    "DNP",
    "Interested",
    "Not Interested",
    "Other",
    "Closed",
    "call back",
    "LOST",
    "buy later",
    "Already a pro user",
    "LOST ONLINE /OFFLINE",
    "REFUSE TO ENGAGE",
    "WAITING FOR COUNSELLING",
    "duplicate",
    "NOT A DOCTOR",
    "Got PG seat",
    "INT - DNP",
]

# The exact string the auto-duplicate-detection uses — must match the real
# "duplicate" stage value (lowercase) in STAGES, not an invented variant, or
# an auto-flagged row's dropdown won't show anything as selected.
DUPLICATE_STAGE = "duplicate"

# Stages that mean the lead is done/resolved and should drop out of the
# overdue/due-today/upcoming follow-up panels.
INACTIVE_STAGES = (
    "Not Interested", "Closed", "LOST", "LOST ONLINE /OFFLINE",
    "REFUSE TO ENGAGE", "NOT A DOCTOR", "Got PG seat", "Already a pro user",
    DUPLICATE_STAGE,
)

# Only these stages generate reminder pings (browser popup + Slack) — and only
# when both a date AND a time are set. A date alone, or any other stage, is
# left for the agent to track on their own; this keeps alerts to the handful
# that actually matter instead of one per follow-up.
ALERT_STAGES = ("Interested", "call back")

LEAD_TYPES = ["Marketing", "Page Landing", "Expiry", "Sign-up"]

# Variant spellings seen in the sheet's "Lead Type" column normalize to the
# canonical value so the dashboard's By Lead Type breakdown stays accurate.
LEAD_TYPE_ALIASES = {
    "sing ups": "Sign-up",
    "signups": "Sign-up",
    "sign ups": "Sign-up",
    "signup": "Sign-up",
}

# Common misspellings seen in the live sheet's "Responsible" column map to the
# one canonical agent name, so assignment still resolves correctly regardless
# of which spelling ends up in a given row.
AGENT_ALIASES = {
    "naveen": "Naveen",
    "sathesh": "Sathesh",
    "sathish": "Sathesh",
    "satish": "Sathesh",
    "satesh": "Sathesh",
    "siva": "Siva",
    "shiva": "Siva",
    "priyanka": "Priyanka",
    "karthika": "Karthika",
    "mehvish": "Mehvish",
    "rohit": "Rohit",
    "anugya": "Anugya",
    "vedant": "Vedant",
    "aboobakkar": "Aboobakkar",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    email TEXT UNIQUE,
    password_hash TEXT,
    role TEXT NOT NULL DEFAULT 'agent',
    full_access INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    slack_webhook_url TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT,
    phone_norm TEXT,
    email TEXT,
    yoa TEXT,
    source TEXT,
    date_added TEXT,
    status TEXT NOT NULL DEFAULT '',
    duplicate_of INTEGER REFERENCES leads(id),
    assigned_agent_id INTEGER REFERENCES agents(id),
    next_action_date TEXT,
    next_action_time TEXT,
    reminder_alerted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id SERIAL PRIMARY KEY,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    agent_id INTEGER REFERENCES agents(id),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_phone_norm ON leads(phone_norm);
"""


class _PGCursor:
    """Wraps a psycopg2 cursor so callers can keep using sqlite3-style `?`
    placeholders and dict-like row access (`row["col"]`) unchanged."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PGConnection:
    """Wraps a psycopg2 connection to mimic the sqlite3.Connection interface
    this app was originally written against: `.execute()` directly on the
    connection, `?` placeholders, dict-style row access, `.commit()`, `.close()`."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query.replace("?", "%s"), tuple(params))
        return _PGCursor(cur)

    def executescript(self, script):
        cur = self._conn.cursor()
        cur.execute(script)
        cur.close()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    raw = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    return _PGConnection(raw)


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def normalize_phone(phone):
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def tel_href(phone):
    """A tel: link opens the phone's own dialer with the number pre-filled —
    standard click-to-call, no integration or service needed."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"tel:+{digits}" if digits else ""


def format_followup(date_str, time_str):
    if not date_str:
        return "—"
    try:
        date_disp = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        date_disp = date_str
    if time_str:
        try:
            time_disp = datetime.strptime(time_str, "%H:%M").strftime("%-I:%M %p")
        except ValueError:
            time_disp = time_str
        return f"{date_disp}, {time_disp}"
    return date_disp

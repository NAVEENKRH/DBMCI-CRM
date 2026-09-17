import csv
import io
import json
import os
import re
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash

import db

app = Flask(__name__)
app.secret_key = os.environ.get("CRM_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals["format_followup"] = db.format_followup
app.jinja_env.globals["tel_href"] = db.tel_href

DEFAULT_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1quM8KYQqZiY-sP6NL0trcoKjEBwRpLSlE432L9SoCYI/export?format=csv&gid=0"
)


# ---------- helpers ----------

def get_agents(active_only=False):
    conn = db.get_db()
    q = "SELECT * FROM agents"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY role, name"
    rows = conn.execute(q).fetchall()
    conn.close()
    return rows


def current_agent():
    agent_id = session.get("agent_id")
    if not agent_id:
        return None
    conn = db.get_db()
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    conn.close()
    return row


def can_edit_lead(agent_row, lead_row):
    if not agent_row:
        return False
    if agent_row["full_access"]:
        return True
    return agent_row["id"] == lead_row["assigned_agent_id"]


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


@app.context_processor
def inject_globals():
    return {
        "all_agents": get_agents(),
        "acting_agent": current_agent(),
        "stages": db.STAGES,
        "lead_types": db.LEAD_TYPES,
        "today": date.today().isoformat(),
    }


def next_round_robin_agent(conn):
    # Only agents (not the manager) participate in round-robin rotation.
    agents = conn.execute(
        "SELECT id FROM agents WHERE active = 1 AND role = 'agent' ORDER BY id"
    ).fetchall()
    if not agents:
        return None
    agent_ids = [a["id"] for a in agents]
    last_id_raw = get_setting(conn, "rr_pointer")
    last_id = int(last_id_raw) if last_id_raw else None
    if last_id in agent_ids:
        idx = (agent_ids.index(last_id) + 1) % len(agent_ids)
    else:
        idx = 0
    next_id = agent_ids[idx]
    set_setting(conn, "rr_pointer", next_id)
    return next_id


def resolve_agent_id_by_name(conn, name):
    if not name:
        return None
    key = name.strip().lower()
    canonical = db.AGENT_ALIASES.get(key)
    if canonical:
        row = conn.execute("SELECT id FROM agents WHERE name = ?", (canonical,)).fetchone()
        if row:
            return row["id"]
    row = conn.execute(
        "SELECT id FROM agents WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))", (name,)
    ).fetchone()
    return row["id"] if row else None


def post_to_slack(webhook_url, text):
    if not webhook_url:
        return False
    try:
        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            webhook_url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def reminder_worker():
    """Background loop (independent of any browser tab): pings Slack once for
    each Interested/Callback lead whose date+time has arrived, then marks it
    alerted so it never fires twice for the same scheduled time. Each agent's
    own personal webhook is used when they've set one (Settings > My Alerts);
    otherwise it falls back to the shared team webhook."""
    while True:
        try:
            conn = db.get_db()
            base_url = get_setting(conn, "public_base_url", "http://127.0.0.1:5050")
            team_webhook = get_setting(conn, "slack_webhook_url", "")
            placeholders = ",".join("?" for _ in db.ALERT_STAGES)
            rows = conn.execute(
                f"SELECT l.*, a.name AS agent_name, a.slack_webhook_url AS agent_webhook "
                f"FROM leads l LEFT JOIN agents a ON a.id = l.assigned_agent_id "
                f"WHERE l.status IN ({placeholders}) AND l.next_action_date IS NOT NULL "
                f"AND l.next_action_time IS NOT NULL AND l.reminder_alerted = 0",
                db.ALERT_STAGES,
            ).fetchall()
            now = datetime.now()
            for r in rows:
                try:
                    dt = datetime.strptime(
                        f"{r['next_action_date']} {r['next_action_time']}", "%Y-%m-%d %H:%M"
                    )
                except ValueError:
                    continue
                if dt <= now:
                    webhook = r["agent_webhook"] or team_webhook
                    if not webhook:
                        continue
                    text = (
                        f":bell: *Follow-up due now* — {r['name']} ({r['phone']})\n"
                        f"Stage: {r['status']} · Responsible: {r['agent_name'] or 'Unassigned'}\n"
                        f"Scheduled: {db.format_followup(r['next_action_date'], r['next_action_time'])}\n"
                        f"<{base_url}/leads/{r['id']}|Open in CRM>"
                    )
                    if post_to_slack(webhook, text):
                        conn.execute(
                            "UPDATE leads SET reminder_alerted = 1 WHERE id = ?", (r["id"],)
                        )
                        conn.commit()
            conn.close()
        except Exception:
            pass
        time.sleep(20)


def normalize_sheet_url(raw_url):
    """Accepts any Google Sheets URL a person might paste (the normal browser
    share link, edit link, or an already-correct export link) and always
    returns the CSV export URL the sync actually needs — so pasting the plain
    link from the address bar just works instead of silently fetching HTML."""
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", raw_url)
    if not m:
        return raw_url
    sheet_id = m.group(1)
    gid_match = re.search(r"[?&#]gid=(\d+)", raw_url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


DATE_ADDED_FORMATS = ("%d/%m/%Y", "%d-%b-%Y")


def parse_sheet_date(raw):
    """The sheet's Date column has used more than one format over time
    (DD/MM/YYYY for older rows, DD-Mon-YYYY for a later historical batch) —
    try each, normalize to ISO, fall back to the raw string if neither fits."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in DATE_ADDED_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


_FOLLOWUP_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_FOLLOWUP_DAY_FIRST = re.compile(
    rf"(\d{{1,2}})\s*(?:st|nd|rd|th)?\s*({_FOLLOWUP_MONTHS})", re.I
)
_FOLLOWUP_MONTH_FIRST = re.compile(rf"({_FOLLOWUP_MONTHS})\s+(\d{{1,2}})\b", re.I)
_FOLLOWUP_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_MONTH_NUM = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


def parse_followup_text(raw, year):
    """Best-effort parse of the sheet's free-text follow-up column (things
    like "17th sept", "Sep 17", "18th 6pm", "post results"). Returns
    (date_iso_or_None, time_hhmm_or_None). Only returns a date when a clear
    day+month was found — ambiguous text ("post results", a bare "16th" with
    no month, a bare time with no date) returns (None, None) rather than
    guessing, so it never silently creates a wrong reminder."""
    raw = (raw or "").strip()
    if not raw:
        return None, None

    m = _FOLLOWUP_DAY_FIRST.search(raw)
    if m:
        day, mon = int(m.group(1)), m.group(2).lower()[:3]
    else:
        m = _FOLLOWUP_MONTH_FIRST.search(raw)
        if m:
            mon, day = m.group(1).lower()[:3], int(m.group(2))
        else:
            return None, None

    month_num = _MONTH_NUM.get(mon)
    if not month_num or not (1 <= day <= 31):
        return None, None
    try:
        date_iso = datetime(year, month_num, day).date().isoformat()
    except ValueError:
        return None, None

    time_str = None
    tm = _FOLLOWUP_TIME.search(raw)
    if tm:
        hour = int(tm.group(1)) % 12
        if tm.group(3).lower() == "pm":
            hour += 12
        minute = int(tm.group(2)) if tm.group(2) else 0
        time_str = f"{hour:02d}:{minute:02d}"

    return date_iso, time_str


def resolve_source(source):
    if not source:
        return source
    return db.LEAD_TYPE_ALIASES.get(source.strip().lower(), source)


def add_lead(
    conn, name, phone, source="", date_added=None, agent_id=None, agent_name=None,
    email=None, yoa=None, status="", next_action_date=None, next_action_time=None,
    remarks=None,
):
    """Insert a lead. If the phone number already exists, the new row is still
    inserted (never merged/dropped) but tagged status='duplicate' with a
    duplicate_of pointer back to the original — the sales team decides later
    what to do with it, nothing is hidden or auto-removed. `status`/`remarks`/
    `next_action_*` let a historical import preserve real prior call outcomes
    instead of every imported lead starting blank."""
    name = (name or "").strip()
    phone = (phone or "").strip()
    phone_norm = db.normalize_phone(phone)
    source = resolve_source(source)
    now = db.now_iso()

    dup_row = None
    if phone_norm:
        dup_row = conn.execute(
            "SELECT id FROM leads WHERE phone_norm = ? AND phone_norm != ''", (phone_norm,)
        ).fetchone()

    if agent_id is None:
        agent_id = resolve_agent_id_by_name(conn, agent_name)
    if agent_id is None:
        agent_id = next_round_robin_agent(conn)

    final_status = db.DUPLICATE_STAGE if dup_row else (status or "")
    duplicate_of = dup_row["id"] if dup_row else None

    cur = conn.execute(
        "INSERT INTO leads (name, phone, phone_norm, email, yoa, source, date_added, "
        "status, duplicate_of, assigned_agent_id, next_action_date, next_action_time, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            name, phone, phone_norm, email or None, yoa or None, source, date_added,
            final_status, duplicate_of, agent_id, next_action_date, next_action_time,
            now, now,
        ),
    )
    lead_id = cur.fetchone()["id"]

    if remarks:
        # Stagger timestamps so remarks display in the same order they were
        # historically added (activities list newest-first).
        base = datetime.now()
        for i, note in enumerate(remarks):
            note = (note or "").strip()
            if not note:
                continue
            ts = (base + timedelta(seconds=i)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO activities (lead_id, agent_id, note, created_at) VALUES (?, ?, ?, ?)",
                (lead_id, agent_id, note, ts),
            )

    return lead_id, bool(dup_row), duplicate_of


# ---------- routes: login ----------

PUBLIC_ENDPOINTS = {"login", "static"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if not session.get("agent_id"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = db.get_db()
        row = conn.execute(
            "SELECT * FROM agents WHERE LOWER(email) = ? AND active = 1", (email,)
        ).fetchone()
        conn.close()
        if row and row["password_hash"] and check_password_hash(row["password_hash"], password):
            session["agent_id"] = row["id"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Incorrect email or password.", "warning")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("agent_id", None)
    return redirect(url_for("login"))


def require_full_access():
    agent = current_agent()
    if not agent or not agent["full_access"]:
        flash("Only a full-access user can do that.", "warning")
        return False
    return True


# ---------- routes: dashboard ----------

def date_bounds(preset, custom_start, custom_end):
    today = date.today()
    if preset == "today":
        return today.isoformat(), today.isoformat()
    if preset == "7":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    if preset == "30":
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    if preset == "custom":
        return custom_start or None, custom_end or None
    return None, None  # "all"


@app.route("/")
def dashboard():
    conn = db.get_db()

    date_preset = request.args.get("date_preset", "today")
    custom_start = request.args.get("start", "")
    custom_end = request.args.get("end", "")
    agent_id = request.args.get("agent_id", "")
    stage = request.args.get("status", "__all__")
    source = request.args.get("source", "")
    followup = request.args.get("followup", "all")

    start_date, end_date = date_bounds(date_preset, custom_start, custom_end)

    conds, params = [], []
    if start_date:
        conds.append("l.date_added >= ?"); params.append(start_date)
    if end_date:
        conds.append("l.date_added <= ?"); params.append(end_date)
    if agent_id:
        conds.append("l.assigned_agent_id = ?"); params.append(agent_id)
    if stage != "__all__":
        conds.append("l.status = ?"); params.append(stage)
    if source:
        conds.append("l.source = ?"); params.append(source)
    where_sql = " AND ".join(conds) if conds else "1=1"

    total_leads = conn.execute(
        f"SELECT COUNT(*) AS c FROM leads l WHERE {where_sql}", params
    ).fetchone()["c"]
    called = conn.execute(
        f"SELECT COUNT(*) AS c FROM leads l WHERE {where_sql} AND l.status NOT IN ('', ?)",
        [*params, db.DUPLICATE_STAGE],
    ).fetchone()["c"]
    not_called = total_leads - called

    # By agent — a LEFT JOIN with filters in the ON clause (not WHERE) so every
    # active agent still shows a row even with zero matching leads.
    join_extra = f" AND {where_sql}".replace("l.", "l2.") if conds else ""
    agent_breakdown = conn.execute(
        f"SELECT a.name AS name, "
        f"COUNT(l2.id) AS total, "
        f"SUM(CASE WHEN l2.status NOT IN ('', ?) THEN 1 ELSE 0 END) AS called "
        f"FROM agents a LEFT JOIN leads l2 ON l2.assigned_agent_id = a.id {join_extra} "
        f"WHERE a.role = 'agent' GROUP BY a.id ORDER BY a.name",
        [db.DUPLICATE_STAGE, *params],
    ).fetchall()

    source_values = " UNION ALL ".join(["SELECT ? AS name"] * len(db.LEAD_TYPES))
    source_breakdown = conn.execute(
        f"SELECT v.name AS source, COUNT(l2.id) AS c FROM ({source_values}) v "
        f"LEFT JOIN leads l2 ON l2.source = v.name {join_extra} "
        f"GROUP BY v.name ORDER BY v.name",
        [*db.LEAD_TYPES, *params],
    ).fetchall()

    stage_values = " UNION ALL ".join(["SELECT ? AS name"] * len(db.STAGES))
    stage_breakdown = conn.execute(
        f"SELECT v.name AS status, COUNT(l2.id) AS c FROM ({stage_values}) v "
        f"LEFT JOIN leads l2 ON l2.status = v.name {join_extra} "
        f"GROUP BY v.name",
        [*db.STAGES, *params],
    ).fetchall()

    followup_leads = []
    if followup != "all":
        today_str = date.today().isoformat()
        fu_conds = list(conds)
        fu_params = list(params)
        fu_conds.append("l.next_action_date IS NOT NULL")
        fu_conds.append(f"l.status NOT IN ({','.join('?' for _ in db.INACTIVE_STAGES)})")
        fu_params.extend(db.INACTIVE_STAGES)
        if followup == "overdue":
            fu_conds.append("l.next_action_date < ?"); fu_params.append(today_str)
        elif followup == "due_today":
            fu_conds.append("l.next_action_date = ?"); fu_params.append(today_str)
        elif followup == "upcoming":
            fu_conds.append("l.next_action_date > ?"); fu_params.append(today_str)
        followup_leads = conn.execute(
            f"SELECT l.*, a.name AS agent_name FROM leads l LEFT JOIN agents a ON a.id = l.assigned_agent_id "
            f"WHERE {' AND '.join(fu_conds)} ORDER BY l.next_action_date",
            fu_params,
        ).fetchall()

    all_agents_for_filter = conn.execute(
        "SELECT * FROM agents WHERE role = 'agent' ORDER BY name"
    ).fetchall()
    conn.close()

    return render_template(
        "dashboard.html",
        total_leads=total_leads, called=called, not_called=not_called,
        agent_breakdown=agent_breakdown, source_breakdown=source_breakdown,
        stage_breakdown=stage_breakdown, followup_leads=followup_leads,
        date_preset=date_preset, custom_start=custom_start, custom_end=custom_end,
        filter_agent=agent_id, filter_status=stage, filter_source=source, filter_followup=followup,
        agents_for_filter=all_agents_for_filter,
    )


# ---------- routes: reminders (exact date+time follow-ups) ----------

@app.route("/api/due-reminders")
def api_due_reminders():
    conn = db.get_db()
    agent = current_agent()
    alert_placeholders = ",".join("?" for _ in db.ALERT_STAGES)
    query = (
        "SELECT l.id, l.name, l.phone, l.next_action_date, l.next_action_time, "
        "a.name AS agent_name FROM leads l LEFT JOIN agents a ON a.id = l.assigned_agent_id "
        "WHERE l.next_action_date IS NOT NULL AND l.next_action_time IS NOT NULL "
        f"AND l.status IN ({alert_placeholders})"
    )
    params = list(db.ALERT_STAGES)
    if agent and not agent["full_access"]:
        query += " AND l.assigned_agent_id = ?"
        params.append(agent["id"])
    rows = conn.execute(query, params).fetchall()
    conn.close()

    now = datetime.now()
    due = []
    for r in rows:
        try:
            dt = datetime.strptime(f"{r['next_action_date']} {r['next_action_time']}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if dt <= now:
            due.append({
                "id": r["id"], "name": r["name"], "phone": r["phone"],
                "agent_name": r["agent_name"],
                "when": db.format_followup(r["next_action_date"], r["next_action_time"]),
            })
    return jsonify(due)


# ---------- routes: leads list ----------

@app.route("/leads")
def leads_list():
    conn = db.get_db()
    status = request.args.get("status", "__all__")
    agent_id = request.args.get("agent_id", "")
    source = request.args.get("source", "")
    search = request.args.get("q", "").strip()

    query = (
        "SELECT l.*, a.name AS agent_name FROM leads l "
        "LEFT JOIN agents a ON a.id = l.assigned_agent_id WHERE 1=1"
    )
    params = []
    if status != "__all__":
        query += " AND l.status = ?"
        params.append(status)
    if agent_id:
        query += " AND l.assigned_agent_id = ?"
        params.append(agent_id)
    if source:
        query += " AND l.source = ?"
        params.append(source)
    if search:
        query += " AND (l.name LIKE ? OR l.phone LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    query += " ORDER BY l.updated_at DESC"

    leads = conn.execute(query, params).fetchall()

    all_sources = conn.execute(
        "SELECT DISTINCT source FROM leads WHERE source IS NOT NULL AND source != '' ORDER BY source"
    ).fetchall()
    all_sources = sorted(set(db.LEAD_TYPES) | {r["source"] for r in all_sources})
    conn.close()

    return render_template(
        "leads.html",
        leads=leads,
        filter_status=status,
        filter_agent=agent_id,
        filter_source=source,
        search=search,
        all_sources=all_sources,
    )


@app.route("/leads/new", methods=["GET", "POST"])
def lead_new():
    if request.method == "POST":
        conn = db.get_db()
        agent_id = request.form.get("assigned_agent_id") or None
        lead_id, was_dup, original_id = add_lead(
            conn,
            name=request.form["name"],
            phone=request.form.get("phone", "").strip(),
            source=request.form.get("source", "").strip(),
            agent_id=int(agent_id) if agent_id else None,
        )
        conn.commit()
        conn.close()
        if was_dup:
            flash(
                f"This mobile number already exists in the system (lead #{original_id}) — "
                f'added as a new row and tagged "duplicate" rather than merged or dropped.',
                "warning",
            )
        else:
            flash("Lead added.", "success")
        return redirect(url_for("lead_detail", lead_id=lead_id))
    return render_template("lead_form.html")


def fetch_lead_context(lead_id, message=None):
    conn = db.get_db()
    row = conn.execute(
        "SELECT l.*, a.name AS agent_name FROM leads l LEFT JOIN agents a ON a.id = l.assigned_agent_id "
        "WHERE l.id = ?",
        (lead_id,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    original = None
    if row["duplicate_of"]:
        original = conn.execute(
            "SELECT l.*, a.name AS agent_name FROM leads l LEFT JOIN agents a ON a.id = l.assigned_agent_id "
            "WHERE l.id = ?",
            (row["duplicate_of"],),
        ).fetchone()
    other_duplicates = conn.execute(
        "SELECT l.*, a.name AS agent_name FROM leads l LEFT JOIN agents a ON a.id = l.assigned_agent_id "
        "WHERE l.duplicate_of = ? OR (l.phone_norm = ? AND l.phone_norm != '' AND l.id != ?)",
        (lead_id, row["phone_norm"], lead_id),
    ).fetchall()
    activities = conn.execute(
        "SELECT act.*, a.name AS agent_name FROM activities act "
        "LEFT JOIN agents a ON a.id = act.agent_id "
        "WHERE act.lead_id = ? ORDER BY act.created_at DESC",
        (lead_id,),
    ).fetchall()
    conn.close()
    editable = can_edit_lead(current_agent(), row)
    return dict(
        lead=row, original=original, editable=editable,
        other_duplicates=other_duplicates, activities=activities, message=message,
    )


@app.route("/leads/<int:lead_id>")
def lead_detail(lead_id):
    ctx = fetch_lead_context(lead_id)
    if ctx is None:
        return "Lead not found", 404
    return render_template("lead_detail.html", **ctx)


@app.route("/leads/<int:lead_id>/fragment")
def lead_fragment(lead_id):
    ctx = fetch_lead_context(lead_id)
    if ctx is None:
        return "Lead not found", 404
    return render_template("_lead_panel_content.html", **ctx)


def is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@app.route("/leads/<int:lead_id>/update", methods=["POST"])
def lead_update(lead_id):
    conn = db.get_db()
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        return "Lead not found", 404
    acting = current_agent()
    if not can_edit_lead(acting, lead):
        conn.close()
        msg = "You don't have permission to edit this lead — it's assigned to someone else."
        if is_ajax():
            return msg, 403
        flash(msg, "warning")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    status = request.form.get("status", "")
    if acting and acting["full_access"]:
        agent_id = request.form.get("assigned_agent_id") or None
    else:
        # Restricted agents can't reassign leads — the "Responsible" field is
        # disabled in their form, and disabled fields don't submit at all, so
        # this must be enforced server-side or every save would null it out.
        agent_id = lead["assigned_agent_id"]
    next_action_date = request.form.get("next_action_date") or None
    next_action_time = request.form.get("next_action_time") or None
    if not next_action_date:
        next_action_time = None  # a time with no date is meaningless

    # Only re-arm the Slack/browser alert if the scheduled date or time
    # actually changed — otherwise an unrelated save (e.g. just a remark)
    # would re-trigger an alert that already fired for the same moment.
    if next_action_date != lead["next_action_date"] or next_action_time != lead["next_action_time"]:
        reminder_alerted = 0
    else:
        reminder_alerted = lead["reminder_alerted"]

    conn.execute(
        "UPDATE leads SET status = ?, assigned_agent_id = ?, next_action_date = ?, "
        "next_action_time = ?, reminder_alerted = ?, updated_at = ? WHERE id = ?",
        (status, agent_id, next_action_date, next_action_time, reminder_alerted, db.now_iso(), lead_id),
    )

    note = request.form.get("note", "").strip()
    if note:
        conn.execute(
            "INSERT INTO activities (lead_id, agent_id, note, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, acting["id"] if acting else None, note, db.now_iso()),
        )
    conn.commit()
    conn.close()

    if is_ajax():
        ctx = fetch_lead_context(lead_id, message="Saved.")
        return render_template("_lead_panel_content.html", **ctx)
    flash("Lead updated.", "success")
    return redirect(url_for("lead_detail", lead_id=lead_id))


@app.route("/leads/<int:lead_id>/cancel-followup", methods=["POST"])
def lead_cancel_followup(lead_id):
    """Clears a scheduled follow-up entirely — used by the "Cancel" button on
    a reminder banner item when the agent decides not to do that follow-up."""
    conn = db.get_db()
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        return "Lead not found", 404
    if not can_edit_lead(current_agent(), lead):
        conn.close()
        return "Not permitted", 403
    conn.execute(
        "UPDATE leads SET next_action_date = NULL, next_action_time = NULL, "
        "reminder_alerted = 0, updated_at = ? WHERE id = ?",
        (db.now_iso(), lead_id),
    )
    conn.commit()
    conn.close()
    return "ok"


# ---------- routes: live sync from the Google Sheet ----------

@app.route("/leads/sync", methods=["GET"])
def leads_sync():
    if not require_full_access():
        return redirect(url_for("dashboard"))
    conn = db.get_db()
    sheet_url = get_setting(conn, "sheet_csv_url", DEFAULT_SHEET_CSV_URL)
    synced_rows = int(get_setting(conn, "sheet_synced_rows", 0))
    conn.close()
    return render_template("leads_sync.html", sheet_url=sheet_url, synced_rows=synced_rows)


@app.route("/leads/sync/run", methods=["POST"])
def leads_sync_run():
    if not require_full_access():
        return redirect(url_for("leads_sync"))
    conn = db.get_db()
    sheet_url = normalize_sheet_url(request.form.get("sheet_url", "").strip() or DEFAULT_SHEET_CSV_URL)
    set_setting(conn, "sheet_csv_url", sheet_url)

    try:
        with urllib.request.urlopen(sheet_url, timeout=20) as resp:
            content = resp.read().decode("utf-8-sig", errors="replace")
    except Exception as e:
        conn.commit()
        conn.close()
        flash(
            f"Couldn't fetch the sheet ({e}). Make sure sharing is set to "
            f"'Anyone with the link — Viewer'.",
            "warning",
        )
        return redirect(url_for("leads_sync"))

    reader = csv.DictReader(io.StringIO(content))
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]

    already_synced = int(get_setting(conn, "sheet_synced_rows", 0))
    new_rows = rows[already_synced:]

    added, duplicates, skipped, invalid = 0, 0, 0, 0
    for r in new_rows:
        name = r.get("Name", "")
        if not name:
            skipped += 1
            continue
        phone = r.get("Numbers", "")
        if len(db.normalize_phone(phone)) < 10:
            # Not a real phone number (e.g. stray text typed into the wrong
            # cell) — skip rather than create an uncallable, dedup-blind lead.
            invalid += 1
            continue
        source = r.get("Lead Type", "")
        date_added = parse_sheet_date(r.get("Date", ""))
        agent_name = r.get("Responsible", "")
        email = r.get("Email", "")
        yoa = r.get("YOA", "")
        status = r.get("Lead stage", "")

        try:
            fallback_year = datetime.strptime(date_added, "%Y-%m-%d").year
        except (ValueError, TypeError):
            fallback_year = datetime.now().year
        followup_raw = r.get("follow up date", "")
        next_action_date, next_action_time = parse_followup_text(followup_raw, fallback_year)

        remarks = [r.get(f"Remark {i}", "") for i in (1, 2, 3, 4)]
        if followup_raw and not next_action_date:
            # Couldn't confidently parse this as a date — keep the original
            # text visible rather than silently dropping it.
            remarks.append(f"(Follow-up noted in sheet, not auto-scheduled: \"{followup_raw}\")")

        _, was_dup, _ = add_lead(
            conn, name=name, phone=phone, source=source,
            date_added=date_added, agent_name=agent_name,
            email=email, yoa=yoa, status=status,
            next_action_date=next_action_date, next_action_time=next_action_time,
            remarks=remarks,
        )
        if was_dup:
            duplicates += 1
        else:
            added += 1

    set_setting(conn, "sheet_synced_rows", len(rows))
    conn.commit()
    conn.close()

    flash(
        f"Synced: {added} new leads added, {duplicates} flagged as duplicates (kept, not merged), "
        f"{skipped} skipped (blank name), {invalid} skipped (not a valid phone number). "
        f"{len(rows) - already_synced} row(s) were new since last sync.",
        "success",
    )
    return redirect(url_for("leads_list"))


# ---------- routes: manual CSV import (fallback / other files) ----------

@app.route("/leads/import", methods=["GET", "POST"])
def leads_import():
    if not require_full_access():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash("Please choose a CSV file.", "warning")
            return redirect(url_for("leads_import"))
        content = file.read().decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(content))
        headers = reader.fieldnames or []
        rows = list(reader)
        session["import_headers"] = headers
        session["import_rows"] = rows
        return redirect(url_for("leads_import_map"))
    return render_template("leads_import.html")


@app.route("/leads/import/map", methods=["GET", "POST"])
def leads_import_map():
    if not require_full_access():
        return redirect(url_for("dashboard"))
    headers = session.get("import_headers")
    rows = session.get("import_rows")
    if not headers or rows is None:
        flash("Upload a CSV first.", "warning")
        return redirect(url_for("leads_import"))

    def guess(*keywords):
        for h in headers:
            for kw in keywords:
                if kw in h.lower():
                    return h
        return ""

    if request.method == "POST":
        name_col = request.form.get("name_col")
        phone_col = request.form.get("phone_col")
        source_col = request.form.get("source_col")
        agent_col = request.form.get("agent_col")
        default_source = request.form.get("default_source", "").strip()

        conn = db.get_db()
        added, duplicates, skipped, invalid = 0, 0, 0, 0
        for r in rows:
            name = (r.get(name_col) or "").strip() if name_col else ""
            if not name:
                skipped += 1
                continue
            phone = (r.get(phone_col) or "").strip() if phone_col else ""
            if len(db.normalize_phone(phone)) < 10:
                invalid += 1
                continue
            source = ((r.get(source_col) or "").strip() if source_col else "") or default_source
            agent_name = (r.get(agent_col) or "").strip() if agent_col else ""
            _, was_dup, _ = add_lead(conn, name, phone, source, agent_name=agent_name)
            if was_dup:
                duplicates += 1
            else:
                added += 1
        conn.commit()
        conn.close()
        session.pop("import_headers", None)
        session.pop("import_rows", None)
        flash(
            f"Import complete: {added} new leads added, {duplicates} flagged as duplicates "
            f"(kept, not merged), {skipped} skipped (no name), {invalid} skipped (not a valid phone number).",
            "success",
        )
        return redirect(url_for("leads_list"))

    return render_template(
        "leads_import_map.html",
        headers=headers,
        row_count=len(rows),
        guess_name=guess("name"),
        guess_phone=guess("phone", "mobile", "number", "contact"),
        guess_source=guess("source", "lead type", "channel"),
        guess_agent=guess("responsible", "agent", "owner"),
    )


# ---------- routes: agents ----------

@app.route("/agents", methods=["GET", "POST"])
def agents_page():
    if not require_full_access():
        return redirect(url_for("dashboard"))
    conn = db.get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "agent")
        if name and email and password:
            try:
                full_access = 1 if role == "manager" else 0
                conn.execute(
                    "INSERT INTO agents (name, email, password_hash, role, full_access) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, email, generate_password_hash(password, method='pbkdf2:sha256'), role, full_access),
                )
                conn.commit()
                flash(f"Added {role} {name} — share their email and password with them to log in.", "success")
            except Exception:
                flash("That name or email already exists.", "warning")
        else:
            flash("Name, email, and password are all required.", "warning")
        conn.close()
        return redirect(url_for("agents_page"))

    rows = conn.execute(
        "SELECT ag.*, "
        "(SELECT COUNT(*) FROM leads l WHERE l.assigned_agent_id = ag.id) AS lead_count "
        "FROM agents ag ORDER BY ag.role, ag.name"
    ).fetchall()
    conn.close()
    return render_template("agents.html", agents=rows)


@app.route("/agents/<int:agent_id>/toggle", methods=["POST"])
def agent_toggle(agent_id):
    if not require_full_access():
        return redirect(url_for("dashboard"))
    conn = db.get_db()
    conn.execute("UPDATE agents SET active = 1 - active WHERE id = ?", (agent_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("agents_page"))


@app.route("/agents/<int:agent_id>/full-access/toggle", methods=["POST"])
def agent_full_access_toggle(agent_id):
    if not require_full_access():
        return redirect(url_for("dashboard"))
    conn = db.get_db()
    conn.execute("UPDATE agents SET full_access = 1 - full_access WHERE id = ?", (agent_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("agents_page"))


@app.route("/agents/<int:agent_id>/reset-password", methods=["POST"])
def agent_reset_password(agent_id):
    if not require_full_access():
        return redirect(url_for("dashboard"))
    new_password = request.form.get("password", "")
    if not new_password:
        flash("Enter a new password.", "warning")
        return redirect(url_for("agents_page"))
    conn = db.get_db()
    conn.execute(
        "UPDATE agents SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password, method='pbkdf2:sha256'), agent_id),
    )
    conn.commit()
    conn.close()
    flash("Password updated — let them know their new password.", "success")
    return redirect(url_for("agents_page"))


# ---------- routes: settings (Slack webhook) ----------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if not require_full_access():
        return redirect(url_for("dashboard"))
    conn = db.get_db()
    if request.method == "POST":
        set_setting(conn, "slack_webhook_url", request.form.get("slack_webhook_url", "").strip())
        set_setting(conn, "public_base_url", request.form.get("public_base_url", "").strip())
        conn.commit()
        conn.close()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))
    slack_webhook_url = get_setting(conn, "slack_webhook_url", "")
    public_base_url = get_setting(conn, "public_base_url", "http://127.0.0.1:5050")
    conn.close()
    return render_template(
        "settings.html", slack_webhook_url=slack_webhook_url, public_base_url=public_base_url
    )


@app.route("/my-alerts", methods=["GET", "POST"])
def my_alerts():
    agent = current_agent()
    conn = db.get_db()
    if request.method == "POST":
        conn.execute(
            "UPDATE agents SET slack_webhook_url = ? WHERE id = ?",
            (request.form.get("slack_webhook_url", "").strip(), agent["id"]),
        )
        conn.commit()
        conn.close()
        flash("Your personal Slack alert link is saved.", "success")
        return redirect(url_for("my_alerts"))
    row = conn.execute(
        "SELECT slack_webhook_url FROM agents WHERE id = ?", (agent["id"],)
    ).fetchone()
    conn.close()
    return render_template("my_alerts.html", slack_webhook_url=row["slack_webhook_url"] or "")


# Runs once when this module is imported — both for local `python3 app.py`
# and for a production WSGI server (gunicorn) that imports `app` directly
# and never executes the `__main__` block below.
db.init_db()
threading.Thread(target=reminder_worker, daemon=True).start()

if __name__ == "__main__":
    debug = os.environ.get("CRM_DEBUG") == "1"
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)

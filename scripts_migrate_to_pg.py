"""One-time migration: copy remaining data from local crm.db (SQLite) into
the Postgres database pointed to by DATABASE_URL. Resumable — skips rows
already present. Uses connection keepalives + batched commits so a stalled
network connection fails fast and is retryable instead of hanging forever."""
import os
import sqlite3
import sys

import psycopg2
import psycopg2.extras

sys.path.insert(0, ".")
import db

BATCH = 200


def pg_connect():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=15,
        keepalives_interval=5,
        keepalives_count=3,
        options="-c statement_timeout=30000",
    )


def main():
    sconn = sqlite3.connect("crm.db")
    sconn.row_factory = sqlite3.Row

    pconn = pg_connect()
    pconn.autocommit = False
    cur = pconn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT COUNT(*) AS c FROM agents")
    print("agents in Postgres:", cur.fetchone()["c"])
    cur.execute("SELECT COUNT(*) AS c FROM leads")
    print("leads in Postgres:", cur.fetchone()["c"])

    cur.execute("SELECT id FROM activities")
    existing_activity_ids = {r["id"] for r in cur.fetchall()}
    print(f"activities already in Postgres: {len(existing_activity_ids)}")

    activities = sconn.execute("SELECT * FROM activities ORDER BY id").fetchall()
    todo = [a for a in activities if a["id"] not in existing_activity_ids]
    print(f"activities left to migrate: {len(todo)} / {len(activities)}")

    for i, act in enumerate(todo):
        cur.execute(
            "INSERT INTO activities (id, lead_id, agent_id, note, created_at) VALUES (%s, %s, %s, %s, %s)",
            (act["id"], act["lead_id"], act["agent_id"], act["note"], act["created_at"]),
        )
        if (i + 1) % BATCH == 0:
            pconn.commit()
            print(f"  committed {i + 1}/{len(todo)}")
    pconn.commit()
    print(f"activities done: {len(todo)} inserted")

    cur.execute("SELECT key FROM settings")
    existing_setting_keys = {r["key"] for r in cur.fetchall()}
    settings = sconn.execute("SELECT * FROM settings").fetchall()
    for s in settings:
        if s["key"] in existing_setting_keys:
            continue
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s)", (s["key"], s["value"]))
    pconn.commit()
    print(f"settings done: {len(settings)} total")

    cur.execute(
        "SELECT setval('activities_id_seq', COALESCE((SELECT MAX(id) FROM activities), 1))"
    )
    cur.execute("SELECT setval('leads_id_seq', COALESCE((SELECT MAX(id) FROM leads), 1))")
    cur.execute("SELECT setval('agents_id_seq', COALESCE((SELECT MAX(id) FROM agents), 1))")
    pconn.commit()

    sconn.close()
    pconn.close()
    print("Done.")


if __name__ == "__main__":
    main()

"""One-time migration: copy all data from local crm.db (SQLite) into the
Postgres database pointed to by DATABASE_URL, preserving every id exactly
(so duplicate_of / assigned_agent_id / lead_id foreign keys stay valid)."""
import sqlite3
import sys

sys.path.insert(0, ".")
import db


def main():
    sconn = sqlite3.connect("crm.db")
    sconn.row_factory = sqlite3.Row

    pconn = db.get_db()
    print("Creating schema in Postgres...")
    pconn.executescript(db.SCHEMA)
    pconn.commit()

    # Wipe any pre-existing rows in Postgres so this script is safely re-runnable.
    pconn.execute("DELETE FROM activities")
    pconn.execute("DELETE FROM leads")
    pconn.execute("DELETE FROM agents")
    pconn.execute("DELETE FROM settings")
    pconn.commit()

    print("Migrating agents...")
    agents = sconn.execute("SELECT * FROM agents ORDER BY id").fetchall()
    for a in agents:
        pconn.execute(
            "INSERT INTO agents (id, name, email, password_hash, role, full_access, active, slack_webhook_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (a["id"], a["name"], a["email"], a["password_hash"], a["role"],
             a["full_access"], a["active"], a["slack_webhook_url"]),
        )
    pconn.commit()
    print(f"  {len(agents)} agents migrated")

    print("Migrating leads...")
    leads = sconn.execute("SELECT * FROM leads ORDER BY id").fetchall()
    for l in leads:
        pconn.execute(
            "INSERT INTO leads (id, name, phone, phone_norm, email, yoa, source, date_added, "
            "status, duplicate_of, assigned_agent_id, next_action_date, next_action_time, "
            "reminder_alerted, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (l["id"], l["name"], l["phone"], l["phone_norm"], l["email"], l["yoa"],
             l["source"], l["date_added"], l["status"], l["duplicate_of"],
             l["assigned_agent_id"], l["next_action_date"], l["next_action_time"],
             l["reminder_alerted"], l["created_at"], l["updated_at"]),
        )
    pconn.commit()
    print(f"  {len(leads)} leads migrated")

    print("Migrating activities...")
    activities = sconn.execute("SELECT * FROM activities ORDER BY id").fetchall()
    for act in activities:
        pconn.execute(
            "INSERT INTO activities (id, lead_id, agent_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (act["id"], act["lead_id"], act["agent_id"], act["note"], act["created_at"]),
        )
    pconn.commit()
    print(f"  {len(activities)} activities migrated")

    print("Migrating settings...")
    settings = sconn.execute("SELECT * FROM settings").fetchall()
    for s in settings:
        pconn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (s["key"], s["value"]))
    pconn.commit()
    print(f"  {len(settings)} settings migrated")

    print("Resetting sequences...")
    for table in ["agents", "leads", "activities"]:
        pconn.execute(
            f"SELECT setval('{table}_id_seq', COALESCE((SELECT MAX(id) FROM {table}), 1))"
        )
    pconn.commit()

    sconn.close()
    pconn.close()
    print("Done.")


if __name__ == "__main__":
    main()

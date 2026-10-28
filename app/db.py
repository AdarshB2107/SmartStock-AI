import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH, DATA_DIR, ROOT

@contextmanager
def connect():
    """Yield a SQLite connection and always close it after the transaction.

    sqlite3.Connection's built-in context manager commits/rolls back but does
    not close the connection. Closing here is important on Windows, where an
    open SQLite handle can prevent regeneration from replacing the database.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript((ROOT / "app" / "schema.sql").read_text(encoding="utf-8"))
        # Lightweight forward-compatible migrations for databases created by
        # earlier project versions. SQLite cannot add these via CREATE TABLE IF NOT EXISTS.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(purchase_recommendation)")}
        migrations = {
            "avg_daily_demand": "REAL NOT NULL DEFAULT 0",
            "demand_source": "TEXT NOT NULL DEFAULT 'historical_fallback'",
            "supplier_lead_time_days": "REAL NOT NULL DEFAULT 0",
            "capacity_before_order": "INTEGER NOT NULL DEFAULT 0",
            "capacity_after_order": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE purchase_recommendation ADD COLUMN {column} {declaration}")

def record_event(conn, entity_type, entity_id, event_type, details, event_time):
    import json
    import hashlib
    details_json = json.dumps(details, sort_keys=True, default=str)
    raw = "|".join([entity_type, entity_id, event_type, event_time, details_json])
    event_id = "EVT-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    conn.execute(
        "INSERT OR IGNORE INTO state_event(event_id,entity_type,entity_id,event_type,event_time,details_json) VALUES(?,?,?,?,?,?)",
        (event_id, entity_type, entity_id, event_type, event_time, details_json)
    )


def reset_generated_tables():
    """Clear generated rows in-place without replacing/deleting the SQLite file.

    Keeping the database file in place avoids Windows WinError 32 when another
    process has the database open. Child tables are cleared before their parents.
    """
    tables_in_delete_order = [
        "purchase_recommendation", "forecast", "sales_history",
        "inventory_snapshot", "lead_time_observation", "reorder_policy",
        "supplier_sku", "state_event", "supplier", "store", "sku",
    ]
    with connect() as conn:
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for table in tables_in_delete_order:
            conn.execute(f"DELETE FROM {table}")

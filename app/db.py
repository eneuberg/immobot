"""SQLite storage. One short-lived connection per call keeps this safe to use from both the
FastAPI threadpool and the Telegram bot's event loop.

Tables:
  listings         one row per listing received from Fredy
  drafts           every generated message, with model, tokens and cost
  settings         user-editable, non-secret settings (key/value strings)
  meta             internal state such as last_webhook_at
  pending_prompts  Telegram "Anpassen" questions waiting for the user's reply
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .settings import DEFAULT_SETTINGS

_db_path: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fredy_id TEXT NOT NULL UNIQUE,
    job_id TEXT,
    provider TEXT,
    title TEXT,
    price TEXT,
    size TEXT,
    address TEXT,
    description TEXT,
    image_url TEXT,
    url TEXT,
    fredy_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    tg_message_id INTEGER,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    kind TEXT NOT NULL,
    instruction TEXT,
    text TEXT NOT NULL,
    hint TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_listing ON drafts(listing_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_prompts (
    prompt_message_id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

LISTING_FIELDS = ["fredy_id", "job_id", "provider", "title", "price", "size", "address", "description", "image_url", "url", "fredy_url"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(data_dir: str) -> None:
    global _db_path
    os.makedirs(data_dir, exist_ok=True)
    _db_path = os.path.join(data_dir, "immobot.db")
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    if _db_path is None:
        raise RuntimeError("db.init_db() was not called")
    conn = sqlite3.connect(_db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# --- listings ---------------------------------------------------------------

def insert_listing(data: dict) -> tuple[int, bool]:
    """Insert a listing from Fredy. Returns (listing_id, created). Known fredy_ids are not duplicated."""
    values = {k: (None if data.get(k) is None else str(data.get(k))) for k in LISTING_FIELDS}
    if not values["fredy_id"]:
        raise ValueError("listing without fredy_id")
    ts = now_iso()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM listings WHERE fredy_id = ?", (values["fredy_id"],)).fetchone()
        if existing:
            return existing["id"], False
        cols = LISTING_FIELDS + ["received_at", "updated_at"]
        cur = conn.execute(
            f"INSERT INTO listings ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [values[k] for k in LISTING_FIELDS] + [ts, ts],
        )
        return cur.lastrowid, True


def get_listing(listing_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone())


def latest_listing() -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM listings ORDER BY id DESC LIMIT 1").fetchone())


def list_listings(status: str | None = None, limit: int = 300) -> list[dict]:
    """Listings newest first, each with draft_count and cost_usd (sum over its drafts)."""
    sql = """
        SELECT l.*, COUNT(d.id) AS draft_count, COALESCE(SUM(d.cost_usd), 0) AS cost_usd
        FROM listings l LEFT JOIN drafts d ON d.listing_id = l.id
        {where}
        GROUP BY l.id ORDER BY l.id DESC LIMIT ?
    """
    params: list = []
    where = ""
    if status:
        where = "WHERE l.status = ?"
        params.append(status)
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql.format(where=where), params).fetchall()]


def set_listing_status(listing_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), listing_id))


def set_listing_tg_message(listing_id: int, message_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET tg_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now_iso(), listing_id))


# --- drafts -----------------------------------------------------------------

def add_draft(
    listing_id: int,
    kind: str,
    text: str,
    model: str,
    *,
    hint: str | None = None,
    instruction: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO drafts (listing_id, kind, instruction, text, hint, model, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, kind, instruction, text, hint, model, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, cost_usd, now_iso()),
        )
        return cur.lastrowid


def latest_draft(listing_id: int, include_test: bool = False) -> dict | None:
    sql = "SELECT * FROM drafts WHERE listing_id = ?" + ("" if include_test else " AND kind != 'test'") + " ORDER BY id DESC LIMIT 1"
    with connect() as conn:
        return _row(conn.execute(sql, (listing_id,)).fetchone())


def list_drafts(listing_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM drafts WHERE listing_id = ? ORDER BY id ASC", (listing_id,)).fetchall()]


def cost_this_month() -> float:
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM drafts WHERE created_at LIKE ?", (month_prefix + "%",)).fetchone()
        return float(row["c"])


def stats() -> dict:
    """Numbers for the dashboard header."""
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    with connect() as conn:
        listings_total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        drafts_total = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        cost_total = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM drafts").fetchone()[0]
        by_status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM listings GROUP BY status")}
    return {
        "listings_total": listings_total,
        "drafts_total": drafts_total,
        "cost_total": float(cost_total),
        "cost_month": cost_this_month(),
        "month": month_prefix,
        "by_status": by_status,
    }


# --- settings & meta --------------------------------------------------------

def get_settings() -> dict[str, str]:
    """All settings, stored values over defaults."""
    with connect() as conn:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def update_settings(values: dict[str, str]) -> None:
    """Store known setting keys only. Unknown keys are ignored (secrets never go here)."""
    with connect() as conn:
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )


def reset_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_meta(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


# --- pending "Anpassen" prompts ---------------------------------------------

def add_pending_prompt(prompt_message_id: int, listing_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_prompts (prompt_message_id, listing_id, created_at) VALUES (?, ?, ?)",
            (prompt_message_id, listing_id, now_iso()),
        )


def pop_pending_prompt(prompt_message_id: int) -> int | None:
    """Return the listing_id waiting on this prompt message and remove the entry."""
    with connect() as conn:
        row = conn.execute("SELECT listing_id FROM pending_prompts WHERE prompt_message_id = ?", (prompt_message_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM pending_prompts WHERE prompt_message_id = ?", (prompt_message_id,))
        return row["listing_id"]

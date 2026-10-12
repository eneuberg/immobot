"""SQLite storage. One short-lived connection per call keeps this safe to use from both the
FastAPI threadpool and the Telegram bot's event loop.

Tables:
  listings         one row per listing received from Fredy (+ ImmoScout24 details, see is24.py)
  listing_media    pictures of a listing downloaded from ImmoScout24 (files under DATA_DIR/media/<listing id>/)
  drafts           every generated message, with provider, model, tokens and cost in the provider's currency
                   (cost_native/currency; euro values are computed with cost_eur_sql, cost_usd is legacy)
  settings         user-editable, non-secret settings (key/value strings)
  meta             internal state such as last_webhook_at and a pending Telegram pairing
  pending_prompts  Telegram "Anpassen" questions waiting for the user's reply
  credentials      API key / tokens entered in the dashboard, AES-GCM encrypted by vault.py
  credential_events  audit log of credential changes (never contains values)
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .settings import DEFAULT_SETTINGS, eur_usd_rate

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
    updated_at TEXT NOT NULL,
    details TEXT,
    contact_name TEXT,
    media_fetched_at TEXT,
    media_error TEXT,
    album_message_id INTEGER,
    album_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS listing_media (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    position INTEGER NOT NULL,
    caption TEXT,
    source_url TEXT NOT NULL,
    file TEXT NOT NULL,
    PRIMARY KEY (listing_id, position)
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
    created_at TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'anthropic',
    currency TEXT NOT NULL DEFAULT 'USD',
    cost_native REAL
);
CREATE INDEX IF NOT EXISTS idx_drafts_listing ON drafts(listing_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_prompts (
    prompt_message_id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (
    name TEXT PRIMARY KEY,
    nonce BLOB NOT NULL,
    ciphertext BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    hint TEXT
);
CREATE TABLE IF NOT EXISTS credential_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    name TEXT NOT NULL,
    action TEXT NOT NULL,
    client_ip TEXT,
    detail TEXT
);
"""

# Columns added after the first deployment. CREATE TABLE IF NOT EXISTS doesn't touch existing tables,
# so init_db adds whatever is missing. Old drafts thereby count as Anthropic / USD.
ADDED_COLUMNS = {
    "listings": [
        ("details", "TEXT"),
        ("contact_name", "TEXT"),
        ("media_fetched_at", "TEXT"),
        ("media_error", "TEXT"),
        ("album_message_id", "INTEGER"),
        ("album_sent_at", "TEXT"),
    ],
    "drafts": [
        ("provider", "TEXT NOT NULL DEFAULT 'anthropic'"),
        ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
        ("cost_native", "REAL"),
    ],
    "credentials": [("hint", "TEXT")],
}

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
        _add_missing_columns(conn)
        _rename_setting(conn, "monthly_budget_usd", "monthly_budget_eur")  # the budget is in euros now (15 stays 15)


def _rename_setting(conn: sqlite3.Connection, old: str, new: str) -> None:
    conn.execute("INSERT OR IGNORE INTO settings (key, value) SELECT ?, value FROM settings WHERE key = ?", (new, old))
    conn.execute("DELETE FROM settings WHERE key = ?", (old,))


def cost_eur_sql(table: str = "") -> str:
    """SQL for a draft's cost in euros; needs the rate (1 € = x $) as its parameter. EUR drafts as billed,
    dollar drafts (Anthropic, and rows from before currencies were stored) divided by the rate."""
    t = table + "." if table else ""
    return (f"(CASE WHEN {t}currency = 'EUR' THEN COALESCE({t}cost_native, 0) "
            f"ELSE COALESCE({t}cost_native, {t}cost_usd) / ? END)")


def _rate() -> float:
    return eur_usd_rate(get_settings())


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration for databases created by an older version."""
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns:
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute("UPDATE drafts SET cost_native = cost_usd WHERE cost_native IS NULL")  # old drafts were USD


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
    """Listings newest first, each with draft_count and cost_eur (sum over its drafts, in euros)."""
    sql = """
        SELECT l.*, COUNT(d.id) AS draft_count, COALESCE(SUM({cost}), 0) AS cost_eur
        FROM listings l LEFT JOIN drafts d ON d.listing_id = l.id
        {where}
        GROUP BY l.id ORDER BY l.id DESC LIMIT ?
    """
    params: list = [_rate()]
    where = ""
    if status:
        where = "WHERE l.status = ?"
        params.append(status)
    params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql.format(cost=cost_eur_sql("d"), where=where), params).fetchall()]


def unannounced_listing_ids(hours: int = 24) -> list[int]:
    """Recent 'new' listings that never got a Telegram follow-up (e.g. bot was down when Fredy posted them)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM listings WHERE status = 'new' AND tg_message_id IS NULL AND received_at >= ? ORDER BY id",
            (since,),
        ).fetchall()
        return [r["id"] for r in rows]


def set_listing_status(listing_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), listing_id))


def set_listing_tg_message(listing_id: int, message_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET tg_message_id = ?, updated_at = ? WHERE id = ?", (message_id, now_iso(), listing_id))


# --- ImmoScout24 details & pictures (see is24.py) ---------------------------

def set_listing_details(listing_id: int, details: str | None, contact_name: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET details = ?, contact_name = ? WHERE id = ?", (details, contact_name, listing_id))


def set_media_state(listing_id: int, fetched_at: str | None, error: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET media_fetched_at = ?, media_error = ? WHERE id = ?", (fetched_at, error, listing_id))


def replace_media(listing_id: int, rows: list[dict]) -> None:
    """Rows: position, caption, source_url, file."""
    with connect() as conn:
        conn.execute("DELETE FROM listing_media WHERE listing_id = ?", (listing_id,))
        conn.executemany(
            "INSERT INTO listing_media (listing_id, position, caption, source_url, file) VALUES (?, ?, ?, ?, ?)",
            [(listing_id, r["position"], r["caption"], r["source_url"], r["file"]) for r in rows],
        )


def list_media(listing_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM listing_media WHERE listing_id = ? ORDER BY position", (listing_id,))]


def thumb_positions() -> dict[int, int]:
    """listing id -> picture for the overview thumbnail: the first one that isn't a floor plan (is24.py sorts
    floor plans first; same words as is24.FLOORPLAN_WORDS), else the first one."""
    sql = """
        SELECT listing_id, COALESCE(MIN(CASE WHEN lower(COALESCE(caption, '')) NOT LIKE '%grundriss%'
                                              AND lower(COALESCE(caption, '')) NOT LIKE '%floorplan%'
                                              AND lower(COALESCE(caption, '')) NOT LIKE '%floor plan%'
                                         THEN position END), MIN(position)) AS position
        FROM listing_media GROUP BY listing_id
    """
    with connect() as conn:
        return {r["listing_id"]: r["position"] for r in conn.execute(sql)}


def get_media(listing_id: int, position: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM listing_media WHERE listing_id = ? AND position = ?", (listing_id, position)).fetchone())


def clear_media(listing_id: int) -> None:
    """Forget the pictures (rows and fetch state); the details text stays."""
    with connect() as conn:
        conn.execute("DELETE FROM listing_media WHERE listing_id = ?", (listing_id,))
        conn.execute("UPDATE listings SET media_fetched_at = NULL, media_error = NULL WHERE id = ?", (listing_id,))


def listings_with_media_received_before(cutoff_iso: str) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id FROM listings WHERE received_at < ?
               AND (media_fetched_at IS NOT NULL OR id IN (SELECT listing_id FROM listing_media))""",
            (cutoff_iso,),
        ).fetchall()
        return [r["id"] for r in rows]


def set_album_sent(listing_id: int, message_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET album_message_id = ?, album_sent_at = ? WHERE id = ?",
                     (message_id, now_iso(), listing_id))


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
    provider: str = "anthropic",
    currency: str = "USD",
    cost_native: float | None = None,
) -> int:
    """cost_usd counts toward budget and totals; cost_native is what the provider bills, in `currency`."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO drafts (listing_id, kind, instruction, text, hint, model, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, cost_usd, created_at,
                                   provider, currency, cost_native)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, kind, instruction, text, hint, model, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, cost_usd, now_iso(),
             provider, currency, cost_usd if cost_native is None else cost_native),
        )
        return cur.lastrowid


def latest_draft(listing_id: int, include_test: bool = False) -> dict | None:
    """The newest draft (with cost_eur)."""
    sql = (f"SELECT *, {cost_eur_sql()} AS cost_eur FROM drafts WHERE listing_id = ?"
           + ("" if include_test else " AND kind != 'test'") + " ORDER BY id DESC LIMIT 1")
    rate = _rate()
    with connect() as conn:
        return _row(conn.execute(sql, (rate, listing_id)).fetchone())


def list_drafts(listing_id: int) -> list[dict]:
    """All drafts of a listing, oldest first (with cost_eur)."""
    sql = f"SELECT *, {cost_eur_sql()} AS cost_eur FROM drafts WHERE listing_id = ? ORDER BY id ASC"
    rate = _rate()
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (rate, listing_id)).fetchall()]


def cost_this_month() -> float:
    """Costs of the current month in euros (the monthly budget is checked against this)."""
    month_prefix, rate = datetime.now(timezone.utc).strftime("%Y-%m"), _rate()
    with connect() as conn:
        row = conn.execute(f"SELECT COALESCE(SUM({cost_eur_sql()}), 0) AS c FROM drafts WHERE created_at LIKE ?",
                           (rate, month_prefix + "%")).fetchone()
        return float(row["c"])


def stats() -> dict:
    """Numbers for the dashboard header; costs in euros."""
    month_prefix, rate = datetime.now(timezone.utc).strftime("%Y-%m"), _rate()
    with connect() as conn:
        listings_total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        drafts_total = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        cost_total = conn.execute(f"SELECT COALESCE(SUM({cost_eur_sql()}), 0) FROM drafts", (rate,)).fetchone()[0]
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


def delete_meta(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


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


# --- credentials (encrypted by vault.py; only bytes are stored here) ------------

def get_credential(name: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM credentials WHERE name = ?", (name,)).fetchone())


def put_credential(name: str, nonce: bytes, ciphertext: bytes, hint: str | None = None) -> None:
    """`hint` is a short display form such as "sk-ant-…A1B2" (never the value)."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO credentials (name, nonce, ciphertext, updated_at, hint) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET nonce = excluded.nonce, ciphertext = excluded.ciphertext,
                                              updated_at = excluded.updated_at, hint = excluded.hint""",
            (name, nonce, ciphertext, now_iso(), hint),
        )


def delete_credential(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM credentials WHERE name = ?", (name,))


def add_credential_event(name: str, action: str, client_ip: str | None, detail: str | None = None) -> None:
    """`detail` is optional context such as the paired chat and user - never a secret value."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO credential_events (at, name, action, client_ip, detail) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), name, action, client_ip, detail),
        )


def last_credential_event(name: str, action: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM credential_events WHERE name = ? AND action = ? ORDER BY id DESC LIMIT 1", (name, action),
        ).fetchone())


def list_credential_events(limit: int = 20) -> list[dict]:
    """Newest first."""
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM credential_events ORDER BY id DESC LIMIT ?", (limit,))]

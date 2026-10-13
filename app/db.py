"""SQLite storage. One short-lived connection per call keeps this safe to use from both the
FastAPI threadpool and the Telegram bot's event loop.

Tables:
  listings         one row per listing received from Fredy (+ ImmoScout24 details and picture source, see media.py)
  listing_media    pictures of a listing, from Fredy or ImmoScout24 (files under DATA_DIR/media/<listing id>/;
                   `file` is "" until the picture is downloaded)
  drafts           every generated message, with provider, model, tokens and cost in the provider's currency
                   (cost_native/currency; euro values are computed with cost_eur_sql, cost_usd is legacy) and where
                   it was asked for (source telegram | dashboard | editor; NULL for drafts from before)
  settings         user-editable, non-secret settings (key/value strings)
  meta             internal state such as last_webhook_at and a pending Telegram pairing
  pending_prompts  Telegram "Anpassen" questions waiting for the user's reply
  credentials      API key / tokens entered in the dashboard, AES-GCM encrypted by vault.py
  credential_events  audit log of credential changes (never contains values)

Learning (see learning.py; the current profile itself stays in `settings`):
  profile_versions   snapshot of the five profile settings (settings.PROFILE_FIELDS) after every change; never deleted
  edit_sessions      one editor session per listing and round: Claude's original, the current and the final text;
                     status open | done | superseded (an open round replaced by a new one from a newer draft)
  feedback           critiques and quick buttons ("Kürzer", ...) from editor, Telegram and dashboard; never test drafts
  learned_rules      rules proposed on "Fertig" and their decisions (proposed / active / inactive / rejected / merged /
                     replaced); update and remove proposals point at their rule with replaces_rule_id; rules the user
                     wrote are active from the start and have no proposed_text
  rewrite_proposals  "Aus Rückmeldungen verbessern": a list of small edits to the profile and what became of them
  llm_usage          tokens and cost of the learning calls (rules, rewrite); counts toward budget and totals like drafts

Editor access from Telegram (see editor_access.py):
  used_login_hashes       SHA-256 of every Telegram login hash used once (replay protection; purged after 1 day)
  editor_links            first successful use of a signed editor link (key = SHA-256 of listing_id:exp:sig)
  editor_access_sessions  browser sessions of the editor: SHA-256 of the cookie value, listing, Telegram user, expiry
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .settings import DEFAULT_SETTINGS, PROFILE_FIELDS, eur_usd_rate

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
    album_sent_at TEXT,
    media_source TEXT
);
CREATE TABLE IF NOT EXISTS listing_media (
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    position INTEGER NOT NULL,
    caption TEXT,
    source_url TEXT NOT NULL,
    file TEXT NOT NULL,
    floor_plan INTEGER NOT NULL DEFAULT 0,
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
    cost_native REAL,
    source TEXT
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
CREATE TABLE IF NOT EXISTS profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edit_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    base_draft_id INTEGER REFERENCES drafts(id),
    original_text TEXT NOT NULL,
    current_text TEXT NOT NULL,
    final_text TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    profile_version_id INTEGER REFERENCES profile_versions(id),
    use_as_example INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    rules_basis TEXT,
    processed_version_id INTEGER REFERENCES profile_versions(id),
    last_draft_id INTEGER REFERENCES drafts(id),
    rules_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_edit_sessions_listing ON edit_sessions(listing_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_sessions_open ON edit_sessions(listing_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    session_id INTEGER REFERENCES edit_sessions(id),
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    before_text TEXT,
    created_at TEXT NOT NULL,
    processed_version_id INTEGER REFERENCES profile_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_listing ON feedback(listing_id);
CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    reason TEXT,
    session_id INTEGER REFERENCES edit_sessions(id),
    replaces_rule_id INTEGER REFERENCES learned_rules(id),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    merged_version_id INTEGER REFERENCES profile_versions(id),
    action TEXT NOT NULL DEFAULT 'add',
    proposed_text TEXT
);
CREATE TABLE IF NOT EXISTS rewrite_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    base_version_id INTEGER NOT NULL REFERENCES profile_versions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    edits TEXT NOT NULL,
    feedback_ids TEXT NOT NULL,
    decided_at TEXT,
    session_ids TEXT NOT NULL DEFAULT '[]',
    applied_version_id INTEGER REFERENCES profile_versions(id)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    listing_id INTEGER REFERENCES listings(id),
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    cost_native REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_listing ON llm_usage(listing_id);
CREATE TABLE IF NOT EXISTS used_login_hashes (
    hash_sha256 TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editor_links (
    link_key TEXT PRIMARY KEY,
    listing_id INTEGER NOT NULL,
    first_used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editor_access_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    telegram_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
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
        ("media_source", "TEXT"),  # "fredy" | "is24" | NULL
    ],
    "listing_media": [("floor_plan", "INTEGER NOT NULL DEFAULT 0")],
    "drafts": [
        ("provider", "TEXT NOT NULL DEFAULT 'anthropic'"),
        ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
        ("cost_native", "REAL"),
        ("source", "TEXT"),  # telegram | dashboard | editor; NULL for older drafts (count as "Entwürfe")
    ],
    "credentials": [("hint", "TEXT")],
    "edit_sessions": [("last_draft_id", "INTEGER REFERENCES drafts(id)"), ("rules_note", "TEXT")],
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
    _ensure_initial_version()


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
    """Listings newest first, each with draft_count and cost_eur (its drafts plus its learning calls in llm_usage,
    in euros)."""
    sql = """
        SELECT l.*, COUNT(d.id) AS draft_count,
               COALESCE(SUM({cost}), 0)
               + (SELECT COALESCE(SUM({usage_cost}), 0) FROM llm_usage u WHERE u.listing_id = l.id) AS cost_eur
        FROM listings l LEFT JOIN drafts d ON d.listing_id = l.id
        {where}
        GROUP BY l.id ORDER BY l.id DESC LIMIT ?
    """
    rate = _rate()
    params: list = [rate, rate]
    where = ""
    if status:
        where = "WHERE l.status = ?"
        params.append(status)
    params.append(limit)
    sql = sql.format(cost=cost_eur_sql("d"), usage_cost=cost_eur_sql("u"), where=where)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


# --- details & pictures (see media.py) -------------------------------------

def set_listing_details(listing_id: int, details: str | None, contact_name: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET details = ?, contact_name = ? WHERE id = ?", (details, contact_name, listing_id))


def set_media_state(listing_id: int, fetched_at: str | None, error: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET media_fetched_at = ?, media_error = ? WHERE id = ?", (fetched_at, error, listing_id))


def replace_media(listing_id: int, rows: list[dict]) -> None:
    """Rows: position, caption, source_url, file ("" = not downloaded yet), optional floor_plan."""
    with connect() as conn:
        conn.execute("DELETE FROM listing_media WHERE listing_id = ?", (listing_id,))
        conn.executemany(
            "INSERT INTO listing_media (listing_id, position, caption, source_url, file, floor_plan) VALUES (?, ?, ?, ?, ?, ?)",
            [(listing_id, r["position"], r["caption"], r["source_url"], r["file"], int(bool(r.get("floor_plan"))))
             for r in rows],
        )


def set_media_file(listing_id: int, position: int, file: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE listing_media SET file = ? WHERE listing_id = ? AND position = ?", (file, listing_id, position))


def set_media_source(listing_id: int, source: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET media_source = ? WHERE id = ?", (source, listing_id))


def list_media(listing_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM listing_media WHERE listing_id = ? ORDER BY position", (listing_id,))]


def thumb_positions() -> dict[int, int]:
    """listing id -> downloaded picture for the overview thumbnail: the first one that isn't a floor plan
    (floor plans come first; flagged, or named like is24.FLOORPLAN_WORDS in older rows), else the first one."""
    sql = """
        SELECT listing_id, COALESCE(MIN(CASE WHEN floor_plan = 0
                                              AND lower(COALESCE(caption, '')) NOT LIKE '%grundriss%'
                                              AND lower(COALESCE(caption, '')) NOT LIKE '%floorplan%'
                                              AND lower(COALESCE(caption, '')) NOT LIKE '%floor plan%'
                                         THEN position END), MIN(position)) AS position
        FROM listing_media WHERE file != '' GROUP BY listing_id
    """
    with connect() as conn:
        return {r["listing_id"]: r["position"] for r in conn.execute(sql)}


def get_media(listing_id: int, position: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM listing_media WHERE listing_id = ? AND position = ?", (listing_id, position)).fetchone())


def clear_media(listing_id: int) -> None:
    """Forget the pictures (rows, source and fetch state); the details text stays."""
    with connect() as conn:
        conn.execute("DELETE FROM listing_media WHERE listing_id = ?", (listing_id,))
        conn.execute("UPDATE listings SET media_fetched_at = NULL, media_error = NULL, media_source = NULL WHERE id = ?",
                     (listing_id,))


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
    source: str | None = None,
) -> int:
    """cost_usd counts toward budget and totals; cost_native is what the provider bills, in `currency`.
    source: where the draft was asked for (telegram | dashboard | editor), for the cost breakdown."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO drafts (listing_id, kind, instruction, text, hint, model, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, cost_usd, created_at,
                                   provider, currency, cost_native, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, kind, instruction, text, hint, model, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, cost_usd, now_iso(),
             provider, currency, cost_usd if cost_native is None else cost_native, source),
        )
        return cur.lastrowid


def latest_draft(listing_id: int, include_test: bool = False) -> dict | None:
    """The newest draft (with cost_eur)."""
    sql = (f"SELECT *, {cost_eur_sql()} AS cost_eur FROM drafts WHERE listing_id = ?"
           + ("" if include_test else " AND kind != 'test'") + " ORDER BY id DESC LIMIT 1")
    rate = _rate()
    with connect() as conn:
        return _row(conn.execute(sql, (rate, listing_id)).fetchone())


def get_draft(draft_id: int) -> dict | None:
    """One draft (with cost_eur)."""
    sql = f"SELECT *, {cost_eur_sql()} AS cost_eur FROM drafts WHERE id = ?"
    rate = _rate()
    with connect() as conn:
        return _row(conn.execute(sql, (rate, draft_id)).fetchone())


def list_drafts(listing_id: int) -> list[dict]:
    """All drafts of a listing, oldest first (with cost_eur)."""
    sql = f"SELECT *, {cost_eur_sql()} AS cost_eur FROM drafts WHERE listing_id = ? ORDER BY id ASC"
    rate = _rate()
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (rate, listing_id)).fetchall()]


# Total cost in euros of drafts plus learning calls (llm_usage); parameters: rate, [prefix,] rate, [prefix].
_COST_SUM_SQL = ("SELECT (SELECT COALESCE(SUM({cost}), 0) FROM drafts{where})"
                 " + (SELECT COALESCE(SUM({cost}), 0) FROM llm_usage{where})")


def current_month() -> str:
    """"2026-10": the budget month (UTC, like the stored times)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def cost_this_month() -> float:
    """Costs of the current month in euros, drafts and learning calls (the monthly budget is checked against this)."""
    month_prefix, rate = current_month(), _rate()
    sql = _COST_SUM_SQL.format(cost=cost_eur_sql(), where=" WHERE created_at LIKE ?")
    with connect() as conn:
        return float(conn.execute(sql, (rate, month_prefix + "%", rate, month_prefix + "%")).fetchone()[0])


COST_SOURCES = ("drafts", "editor", "rules", "rewrite", "test", "other")
# A month's costs by source, in euros; parameters: rate, month prefix, rate, month prefix.
_COST_BY_SOURCE_SQL = """
    SELECT CASE WHEN kind = 'test' THEN 'test'
                WHEN source = 'editor' AND kind != 'initial' THEN 'editor'
                ELSE 'drafts' END AS part, SUM({cost}) AS eur
    FROM drafts WHERE created_at LIKE ? GROUP BY part
    UNION ALL
    SELECT CASE WHEN purpose IN ('rules', 'rewrite') THEN purpose ELSE 'other' END AS part, SUM({cost}) AS eur
    FROM llm_usage WHERE created_at LIKE ? GROUP BY part
"""


def cost_by_source(month: str | None = None) -> dict[str, float]:
    """A month's costs ("2026-10", default the current one) in euros by source, every COST_SOURCES key present:
    drafts (Telegram / dashboard, and drafts from before sources were stored), editor (revisions in the editor; its
    "Entwurf erzeugen" is a draft), test (test drafts from the settings), rules / rewrite (learning calls in
    llm_usage), other (llm_usage with another purpose). Sums up to cost_this_month() for the current month."""
    month_prefix, rate = month or current_month(), _rate()
    parts = dict.fromkeys(COST_SOURCES, 0.0)
    with connect() as conn:
        for row in conn.execute(_COST_BY_SOURCE_SQL.format(cost=cost_eur_sql()),
                                (rate, month_prefix + "%", rate, month_prefix + "%")):
            parts[row["part"]] += float(row["eur"] or 0)
    return parts


def stats() -> dict:
    """Numbers for the dashboard header; costs in euros (drafts and learning calls)."""
    month_prefix, rate = current_month(), _rate()
    with connect() as conn:
        listings_total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        drafts_total = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
        cost_total = conn.execute(_COST_SUM_SQL.format(cost=cost_eur_sql(), where=""), (rate, rate)).fetchone()[0]
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


# --- learning: shared helpers -------------------------------------------------

def _update(table: str, allowed: set[str], row_id: int, fields: dict) -> None:
    """UPDATE one row by id. Column names come from code, never from a request: unknown ones are a bug."""
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown {table} columns: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connect() as conn:
        conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", [*fields.values(), row_id])


def _json_list(raw) -> list:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


# Listing columns joined onto feedback, sessions and rules (for the "Inserats-Kurzinfo" and origin links).
_LISTING_INFO = ("l.title AS listing_title, l.provider AS listing_provider, l.price AS listing_price, "
                 "l.size AS listing_size, l.address AS listing_address")


# --- profile versions ---------------------------------------------------------

def current_profile_values() -> dict[str, str]:
    """The effective values of the five profile settings (stored over defaults), as stored strings."""
    current = get_settings()
    return {key: current[key] for key in PROFILE_FIELDS}


def _version(row: sqlite3.Row | None) -> dict | None:
    """A profile_versions row with `data` decoded to {field: value} (every PROFILE_FIELDS key present)."""
    if row is None:
        return None
    version = dict(row)
    try:
        data = json.loads(version["data"])
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    version["data"] = {key: str(data.get(key) or "") for key in PROFILE_FIELDS}
    return version


def add_version(source: str, data: dict, note: str | None = None) -> int:
    """Store a snapshot unconditionally (snapshot_profile() skips unchanged ones)."""
    payload = json.dumps({key: str(data.get(key) or "") for key in PROFILE_FIELDS}, ensure_ascii=False)
    with connect() as conn:
        cur = conn.execute("INSERT INTO profile_versions (created_at, source, note, data) VALUES (?, ?, ?, ?)",
                           (now_iso(), source, note, payload))
        return cur.lastrowid


def get_version(version_id: int) -> dict | None:
    with connect() as conn:
        return _version(conn.execute("SELECT * FROM profile_versions WHERE id = ?", (version_id,)).fetchone())


def latest_version() -> dict | None:
    with connect() as conn:
        return _version(conn.execute("SELECT * FROM profile_versions ORDER BY id DESC LIMIT 1").fetchone())


def list_versions(limit: int | None = None) -> list[dict]:
    """Newest first, each with decoded `data`."""
    sql = "SELECT * FROM profile_versions ORDER BY id DESC" + (" LIMIT ?" if limit else "")
    with connect() as conn:
        return [_version(r) for r in conn.execute(sql, (limit,) if limit else ())]


def snapshot_profile(source: str, note: str | None = None) -> int | None:
    """Version the current profile values. Returns the new version id, or None when they equal the latest
    version (a save without a real change creates no version). source: initial | manual | reset | feedback | restore."""
    values = current_profile_values()
    latest = latest_version()
    if latest is not None and latest["data"] == values:
        return None
    return add_version(source, values, note)


def restore_version(version_id: int) -> int | None:
    """Write the five profile settings of a version back and record that as a new version (source "restore",
    note "aus Version #n"). Returns the new version id, or None if the profile already had these values.
    Raises LookupError for an unknown version."""
    version = get_version(version_id)
    if version is None:
        raise LookupError("Version nicht gefunden.")
    update_settings(version["data"])
    return snapshot_profile("restore", f"aus Version #{version_id}")


def _ensure_initial_version() -> None:
    """First start with versioning (new or older database): the current values become version "initial"."""
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM profile_versions LIMIT 1").fetchone()
    if not exists:
        add_version("initial", current_profile_values())


# --- edit sessions --------------------------------------------------------------

SESSION_UPDATE_COLUMNS = {"current_text", "final_text", "status", "use_as_example", "finished_at", "rules_basis",
                          "processed_version_id", "last_draft_id", "rules_note"}


def create_session(listing_id: int, base_draft_id: int | None, text: str, profile_version_id: int | None) -> int:
    """A new open session with `text` as original and current text. last_draft_id (the newest draft the session
    knows: its base, later its latest editor revision) starts as base_draft_id. Raises sqlite3.IntegrityError when
    the listing already has an open session (unique index idx_edit_sessions_open)."""
    ts = now_iso()
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO edit_sessions (listing_id, base_draft_id, original_text, current_text, status,
                                          profile_version_id, created_at, updated_at, last_draft_id)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (listing_id, base_draft_id, text, text, profile_version_id, ts, ts, base_draft_id),
        )
        return cur.lastrowid


def get_session(session_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM edit_sessions WHERE id = ?", (session_id,)).fetchone())


def open_session(listing_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM edit_sessions WHERE listing_id = ? AND status = 'open'",
                                 (listing_id,)).fetchone())


def latest_session(listing_id: int, status: str | None = None) -> dict | None:
    """The listing's newest session (any status), or its newest one with `status`."""
    sql = "SELECT * FROM edit_sessions WHERE listing_id = ?" + (" AND status = ?" if status else "")
    with connect() as conn:
        return _row(conn.execute(sql + " ORDER BY id DESC LIMIT 1",
                                 (listing_id, status) if status else (listing_id,)).fetchone())


def update_session(session_id: int, **fields) -> None:
    """Set columns of SESSION_UPDATE_COLUMNS; updated_at is set too. Raises sqlite3.IntegrityError when
    status 'open' would give the listing a second open session."""
    _update("edit_sessions", SESSION_UPDATE_COLUMNS | {"updated_at"}, session_id, {**fields, "updated_at": now_iso()})


def example_sessions(limit: int | None = 3, use: bool = True) -> list[dict]:
    """Done sessions marked use_as_example, newest finished first, with listing_title / listing_address
    (the "## Beispiele" in the system prompt and the list of used examples). use=False: the ones taken out with
    "Nicht mehr als Beispiel nutzen" (Verhalten → Beispiele, to switch them on again)."""
    sql = (f"SELECT s.*, {_LISTING_INFO} FROM edit_sessions s JOIN listings l ON l.id = s.listing_id "
           "WHERE s.status = 'done' AND s.use_as_example = ? AND TRIM(COALESCE(s.final_text, '')) != '' "
           "ORDER BY s.finished_at DESC, s.id DESC" + (" LIMIT ?" if limit else ""))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (1 if use else 0, limit) if limit else (1 if use else 0,))]


def unprocessed_sessions(limit: int | None = None) -> list[dict]:
    """Done sessions whose final text differs from Claude's original and that no profile rewrite has processed
    yet, newest finished first, with listing info."""
    sql = (f"SELECT s.*, {_LISTING_INFO} FROM edit_sessions s JOIN listings l ON l.id = s.listing_id "
           "WHERE s.status = 'done' AND s.processed_version_id IS NULL AND s.final_text IS NOT NULL "
           "AND s.final_text != s.original_text ORDER BY s.finished_at DESC, s.id DESC" + (" LIMIT ?" if limit else ""))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (limit,) if limit else ())]


def mark_sessions_processed(session_ids: list[int], version_id: int) -> None:
    if not session_ids:
        return
    with connect() as conn:
        conn.execute(f"UPDATE edit_sessions SET processed_version_id = ? WHERE id IN ({_placeholders(session_ids)}) "
                     "AND processed_version_id IS NULL", [version_id, *session_ids])


# --- feedback -------------------------------------------------------------------

def add_feedback(listing_id: int, source: str, kind: str, text: str, *, session_id: int | None = None,
                 before_text: str | None = None) -> int:
    """source: editor | telegram | dashboard; kind: critique (text = the critique verbatim), quick
    (text = shorter | formal | regenerate) or note (editor: a critique typed but not sent to Claude before "Fertig").
    before_text: the text the critique was about (editor only)."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO feedback (listing_id, session_id, source, kind, text, before_text, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (listing_id, session_id, source, kind, text, before_text, now_iso()),
        )
        return cur.lastrowid


def list_feedback(*, unprocessed: bool = False, session_id: int | None = None, kind: str | None = None,
                  limit: int | None = None) -> list[dict]:
    """Newest first, with listing info (listing_title, listing_provider, listing_price, listing_size,
    listing_address)."""
    where, params = [], []
    if unprocessed:
        where.append("f.processed_version_id IS NULL")
    if session_id is not None:
        where.append("f.session_id = ?")
        params.append(session_id)
    if kind is not None:
        where.append("f.kind = ?")
        params.append(kind)
    sql = (f"SELECT f.*, {_LISTING_INFO} FROM feedback f JOIN listings l ON l.id = f.listing_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY f.id DESC" + (" LIMIT ?" if limit else ""))
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, [*params, limit] if limit else params)]


def quick_feedback_counts(unprocessed: bool = True) -> dict[str, int]:
    """{"shorter": 7, "regenerate": 2, ...} of the quick buttons."""
    sql = ("SELECT text, COUNT(*) AS n FROM feedback WHERE kind = 'quick'"
           + (" AND processed_version_id IS NULL" if unprocessed else "") + " GROUP BY text")
    with connect() as conn:
        return {r["text"]: r["n"] for r in conn.execute(sql)}


def count_unprocessed_feedback() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM feedback WHERE processed_version_id IS NULL").fetchone()[0]


def mark_feedback_processed(feedback_ids: list[int], version_id: int) -> None:
    if not feedback_ids:
        return
    with connect() as conn:
        conn.execute(f"UPDATE feedback SET processed_version_id = ? WHERE id IN ({_placeholders(feedback_ids)}) "
                     "AND processed_version_id IS NULL", [version_id, *feedback_ids])


def count_feedback_since(listing_id: int, source: str, since_iso: str, kind: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM feedback WHERE listing_id = ? AND source = ? AND created_at >= ?"
    with connect() as conn:
        if kind is None:
            return conn.execute(sql, (listing_id, source, since_iso)).fetchone()[0]
        return conn.execute(sql + " AND kind = ?", (listing_id, source, since_iso, kind)).fetchone()[0]


# --- learned rules --------------------------------------------------------------

RULE_UPDATE_COLUMNS = {"text", "status", "reason", "decided_at", "merged_version_id"}


def add_rule(text: str, *, status: str = "proposed", action: str = "add", reason: str | None = None,
             session_id: int | None = None, replaces_rule_id: int | None = None, manual: bool = False) -> int:
    """action: add | update | remove (what the proposal does; replaces_rule_id = the rule it changes).
    proposed_text keeps Claude's wording when the text is edited before accepting; NULL for a rule the user wrote
    (manual)."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO learned_rules (text, status, reason, session_id, replaces_rule_id, created_at, action,
                                          proposed_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (text, status, reason, session_id, replaces_rule_id, now_iso(), action, None if manual else text),
        )
        return cur.lastrowid


_RULE_SELECT = (f"SELECT r.*, s.listing_id AS listing_id, {_LISTING_INFO} FROM learned_rules r "
                "LEFT JOIN edit_sessions s ON s.id = r.session_id LEFT JOIN listings l ON l.id = s.listing_id")


def get_rule(rule_id: int) -> dict | None:
    """A rule with its origin (listing_id, listing_title, ... of its session; NULL without session)."""
    with connect() as conn:
        return _row(conn.execute(_RULE_SELECT + " WHERE r.id = ?", (rule_id,)).fetchone())


def list_rules(statuses=None, *, session_id: int | None = None, newest_first: bool = False,
               limit: int | None = None) -> list[dict]:
    """Rules (with origin, like get_rule), oldest first unless newest_first (by decision time, then id)."""
    where, params = [], []
    if statuses:
        statuses = [statuses] if isinstance(statuses, str) else list(statuses)
        where.append(f"r.status IN ({_placeholders(statuses)})")
        params += statuses
    if session_id is not None:
        where.append("r.session_id = ?")
        params.append(session_id)
    order = " ORDER BY COALESCE(r.decided_at, r.created_at) DESC, r.id DESC" if newest_first else " ORDER BY r.id"
    sql = _RULE_SELECT + (" WHERE " + " AND ".join(where) if where else "") + order + (" LIMIT ?" if limit else "")
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, [*params, limit] if limit else params)]


def count_rules(status: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM learned_rules WHERE status = ?", (status,)).fetchone()[0]


def update_rule(rule_id: int, **fields) -> None:
    _update("learned_rules", RULE_UPDATE_COLUMNS, rule_id, fields)


def delete_rule(rule_id: int) -> None:
    """Delete a rule and the open proposals that would change it; later rules that replaced it keep their text."""
    with connect() as conn:
        conn.execute("DELETE FROM learned_rules WHERE replaces_rule_id = ? AND status = 'proposed'", (rule_id,))
        conn.execute("UPDATE learned_rules SET replaces_rule_id = NULL WHERE replaces_rule_id = ?", (rule_id,))
        conn.execute("DELETE FROM learned_rules WHERE id = ?", (rule_id,))


def delete_proposed_rules(session_id: int) -> None:
    """Undecided proposals of a session (replaced by a new round of proposals)."""
    with connect() as conn:
        conn.execute("DELETE FROM learned_rules WHERE session_id = ? AND status = 'proposed'", (session_id,))


def merge_rules(rule_ids: list[int], version_id: int) -> None:
    """Active or inactive rules that a profile rewrite took over become 'merged'."""
    if not rule_ids:
        return
    with connect() as conn:
        conn.execute(f"UPDATE learned_rules SET status = 'merged', merged_version_id = ?, decided_at = ? "
                     f"WHERE id IN ({_placeholders(rule_ids)}) AND status IN ('active', 'inactive')",
                     [version_id, now_iso(), *rule_ids])


# --- rewrite proposals ------------------------------------------------------------

PROPOSAL_UPDATE_COLUMNS = {"status", "edits", "decided_at", "applied_version_id"}


def _proposal(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    proposal = dict(row)
    for key in ("edits", "feedback_ids", "session_ids"):
        proposal[key] = _json_list(proposal.get(key))
    return proposal


def add_rewrite_proposal(base_version_id: int, edits: list[dict], feedback_ids: list[int],
                         session_ids: list[int]) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO rewrite_proposals (created_at, base_version_id, status, edits, feedback_ids, session_ids)
               VALUES (?, ?, 'pending', ?, ?, ?)""",
            (now_iso(), base_version_id, json.dumps(edits, ensure_ascii=False), json.dumps(feedback_ids),
             json.dumps(session_ids)),
        )
        return cur.lastrowid


def get_rewrite_proposal(proposal_id: int) -> dict | None:
    """With edits, feedback_ids and session_ids decoded to lists."""
    with connect() as conn:
        return _proposal(conn.execute("SELECT * FROM rewrite_proposals WHERE id = ?", (proposal_id,)).fetchone())


def list_rewrite_proposals(statuses=None, limit: int | None = None) -> list[dict]:
    """Newest first, decoded like get_rewrite_proposal."""
    where, params = "", []
    if statuses:
        statuses = [statuses] if isinstance(statuses, str) else list(statuses)
        where = f" WHERE status IN ({_placeholders(statuses)})"
        params = statuses
    sql = "SELECT * FROM rewrite_proposals" + where + " ORDER BY id DESC" + (" LIMIT ?" if limit else "")
    with connect() as conn:
        return [_proposal(r) for r in conn.execute(sql, [*params, limit] if limit else params)]


def update_rewrite_proposal(proposal_id: int, **fields) -> None:
    """`edits` may be given as a list (stored as JSON)."""
    if isinstance(fields.get("edits"), list):
        fields["edits"] = json.dumps(fields["edits"], ensure_ascii=False)
    _update("rewrite_proposals", PROPOSAL_UPDATE_COLUMNS, proposal_id, fields)


# --- llm_usage (learning calls) -----------------------------------------------------

def add_llm_usage(purpose: str, provider: str, model: str, *, listing_id: int | None = None, input_tokens: int = 0,
                  output_tokens: int = 0, cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                  cost_usd: float = 0.0, currency: str = "USD", cost_native: float | None = None) -> int:
    """purpose: rules | rewrite. Costs like add_draft: cost_native in `currency`, cost_usd for the legacy column."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO llm_usage (created_at, listing_id, purpose, provider, model, input_tokens, output_tokens,
                                      cache_read_tokens, cache_write_tokens, cost_usd, currency, cost_native)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), listing_id, purpose, provider, model, input_tokens, output_tokens, cache_read_tokens,
             cache_write_tokens, cost_usd, currency, cost_usd if cost_native is None else cost_native),
        )
        return cur.lastrowid


def list_llm_usage(limit: int = 50) -> list[dict]:
    """Newest first (with cost_eur)."""
    sql = f"SELECT *, {cost_eur_sql()} AS cost_eur FROM llm_usage ORDER BY id DESC LIMIT ?"
    rate = _rate()
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (rate, limit))]


def count_llm_usage_since(listing_id: int, purpose: str, since_iso: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM llm_usage WHERE listing_id = ? AND purpose = ? AND created_at >= ?",
                            (listing_id, purpose, since_iso)).fetchone()[0]


# --- editor access from Telegram (editor_access.py; all times are ISO strings like now_iso()) -------------

def use_login_hash(hash_sha256: str, now: str, purge_before: str) -> bool:
    """Record a Telegram login hash as used. False if it was used before. Entries older than purge_before go."""
    with connect() as conn:
        conn.execute("DELETE FROM used_login_hashes WHERE created_at < ?", (purge_before,))
        cur = conn.execute("INSERT OR IGNORE INTO used_login_hashes (hash_sha256, created_at) VALUES (?, ?)",
                           (hash_sha256, now))
        return cur.rowcount == 1


def editor_link_first_use(link_key: str, listing_id: int, now: str, purge_before: str) -> str:
    """first_used_at of a signed link, recorded as `now` on its first use. Links first used before purge_before
    are forgotten (their token has expired by then anyway)."""
    with connect() as conn:
        conn.execute("DELETE FROM editor_links WHERE first_used_at < ?", (purge_before,))
        conn.execute("INSERT OR IGNORE INTO editor_links (link_key, listing_id, first_used_at) VALUES (?, ?, ?)",
                     (link_key, listing_id, now))
        return conn.execute("SELECT first_used_at FROM editor_links WHERE link_key = ?", (link_key,)).fetchone()[0]


def add_editor_access_session(token_hash: str, listing_id: int, telegram_user_id: int, created_at: str,
                              expires_at: str, purge_before: str) -> int:
    """A new editor browser session; sessions that expired before purge_before are removed."""
    with connect() as conn:
        conn.execute("DELETE FROM editor_access_sessions WHERE expires_at < ?", (purge_before,))
        cur = conn.execute(
            """INSERT INTO editor_access_sessions (token_hash, listing_id, telegram_user_id, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_hash, listing_id, telegram_user_id, created_at, expires_at),
        )
        return cur.lastrowid


def get_editor_access_session(token_hash: str) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM editor_access_sessions WHERE token_hash = ?", (token_hash,)).fetchone())

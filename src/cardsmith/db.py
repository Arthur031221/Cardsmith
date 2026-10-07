"""SQLite storage for decks, cards, and the SM-2 review schedule."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cardsmith import sm2
from cardsmith.llm.schema import Card

SCHEMA = """
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_filename TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
    card_type TEXT NOT NULL,
    front TEXT NOT NULL DEFAULT '',
    back TEXT NOT NULL DEFAULT '',
    cloze_text TEXT NOT NULL DEFAULT '',
    source_quote TEXT NOT NULL DEFAULT '',
    source_location TEXT NOT NULL DEFAULT '',
    grounded INTEGER NOT NULL DEFAULT 1,
    tags TEXT NOT NULL DEFAULT '[]',
    repetition INTEGER NOT NULL DEFAULT 0,
    ease_factor REAL NOT NULL DEFAULT 2.5,
    interval_days INTEGER NOT NULL DEFAULT 0,
    due_at TEXT NOT NULL,
    last_reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    quality INTEGER NOT NULL,
    reviewed_at TEXT NOT NULL,
    interval_after INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck_id);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(deck_id, due_at);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _card_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["grounded"] = bool(d["grounded"])
    return d


# -- decks --------------------------------------------------------------


def create_deck(conn: sqlite3.Connection, title: str, source_filename: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO decks (title, source_filename, created_at) VALUES (?, ?, ?)",
        (title, source_filename, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_decks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM decks ORDER BY created_at DESC").fetchall()
    decks = []
    now = now_iso()
    for row in rows:
        d = dict(row)
        counts = conn.execute(
            "SELECT COUNT(*) AS total, SUM(due_at <= ?) AS due FROM cards WHERE deck_id = ?",
            (now, d["id"]),
        ).fetchone()
        d["card_count"] = counts["total"] or 0
        d["due_count"] = counts["due"] or 0
        decks.append(d)
    return decks


def get_deck(conn: sqlite3.Connection, deck_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    return dict(row) if row else None


def delete_deck(conn: sqlite3.Connection, deck_id: int) -> None:
    conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
    conn.commit()


# -- cards ----------------------------------------------------------------


def add_cards(conn: sqlite3.Connection, deck_id: int, cards: list[Card]) -> list[dict]:
    """Insert generated cards into a deck, due immediately (new cards)."""
    created = now_iso()
    out = []
    for card in cards:
        cur = conn.execute(
            """INSERT INTO cards
               (deck_id, card_type, front, back, cloze_text, source_quote, source_location,
                grounded, tags, repetition, ease_factor, interval_days, due_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)""",
            (
                deck_id,
                card.card_type,
                card.front,
                card.back,
                card.cloze_text,
                card.source_quote,
                card.source_location,
                int(card.grounded),
                json.dumps(card.tags),
                sm2.DEFAULT_EASE_FACTOR,
                created,
                created,
            ),
        )
        out.append(get_card(conn, cur.lastrowid))
    conn.commit()
    return out


def add_card(conn: sqlite3.Connection, deck_id: int, card: Card) -> dict:
    return add_cards(conn, deck_id, [card])[0]


def list_cards(conn: sqlite3.Connection, deck_id: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM cards WHERE deck_id = ? ORDER BY id", (deck_id,)).fetchall()
    return [_card_row_to_dict(r) for r in rows]


def get_card(conn: sqlite3.Connection, card_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _card_row_to_dict(row) if row else None


EDITABLE_FIELDS = {"front", "back", "cloze_text", "source_quote", "card_type", "tags"}


def update_card(conn: sqlite3.Connection, card_id: int, fields: dict) -> dict | None:
    updates = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not updates:
        return get_card(conn, card_id)
    if "tags" in updates:
        updates["tags"] = json.dumps(updates["tags"])
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE cards SET {set_clause} WHERE id = ?", (*updates.values(), card_id))
    conn.commit()
    return get_card(conn, card_id)


def delete_card(conn: sqlite3.Connection, card_id: int) -> None:
    conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()


# -- study ------------------------------------------------------------------


def next_due_card(conn: sqlite3.Connection, deck_id: int, now: str | None = None) -> dict | None:
    now = now or now_iso()
    row = conn.execute(
        "SELECT * FROM cards WHERE deck_id = ? AND due_at <= ? ORDER BY due_at LIMIT 1",
        (deck_id, now),
    ).fetchone()
    return _card_row_to_dict(row) if row else None


def deck_stats(conn: sqlite3.Connection, deck_id: int, now: str | None = None) -> dict:
    now = now or now_iso()
    row = conn.execute(
        "SELECT COUNT(*) AS total, SUM(due_at <= ?) AS due, SUM(repetition = 0) AS new "
        "FROM cards WHERE deck_id = ?",
        (now, deck_id),
    ).fetchone()
    return {"total": row["total"] or 0, "due": row["due"] or 0, "new": row["new"] or 0}


def record_review(
    conn: sqlite3.Connection, card_id: int, quality: int, now: str | None = None
) -> dict | None:
    card = get_card(conn, card_id)
    if card is None:
        return None
    now = now or now_iso()
    state = sm2.SM2State(
        repetition=card["repetition"],
        ease_factor=card["ease_factor"],
        interval_days=card["interval_days"],
    )
    new_state = sm2.review(state, quality)
    due_at = (
        (datetime.fromisoformat(now) + timedelta(days=new_state.interval_days))
        .astimezone(UTC)
        .isoformat()
    )
    conn.execute(
        """UPDATE cards SET repetition = ?, ease_factor = ?, interval_days = ?,
           due_at = ?, last_reviewed_at = ? WHERE id = ?""",
        (
            new_state.repetition,
            new_state.ease_factor,
            new_state.interval_days,
            due_at,
            now,
            card_id,
        ),
    )
    conn.execute(
        "INSERT INTO reviews (card_id, quality, reviewed_at, interval_after) VALUES (?, ?, ?, ?)",
        (card_id, quality, now, new_state.interval_days),
    )
    conn.commit()
    return get_card(conn, card_id)

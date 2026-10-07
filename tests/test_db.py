from datetime import UTC, datetime, timedelta

from cardsmith import db
from cardsmith.llm.schema import Card


def make_card(front="Q", back="A", quote="quote text", loc="page 1"):
    return Card(
        card_type="basic",
        front=front,
        back=back,
        cloze_text="",
        source_quote=quote,
        source_location=loc,
    )


def test_create_deck_and_add_cards(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology", "chapter4.pdf")
    saved = db.add_cards(conn, deck_id, [make_card(), make_card(front="Q2", back="A2")])
    assert len(saved) == 2
    assert all(c["due_at"] for c in saved)
    assert saved[0]["repetition"] == 0

    cards = db.list_cards(conn, deck_id)
    assert len(cards) == 2
    assert cards[0]["tags"] == []


def test_list_decks_reports_counts(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    db.add_cards(conn, deck_id, [make_card(), make_card(front="Q2", back="A2")])
    decks = db.list_decks(conn)
    assert len(decks) == 1
    assert decks[0]["card_count"] == 2
    assert decks[0]["due_count"] == 2  # new cards are due immediately


def test_update_and_delete_card(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    card = db.add_card(conn, deck_id, make_card())
    updated = db.update_card(conn, card["id"], {"front": "New question"})
    assert updated["front"] == "New question"

    db.delete_card(conn, card["id"])
    assert db.get_card(conn, card["id"]) is None


def test_delete_deck_cascades_to_cards(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    card = db.add_card(conn, deck_id, make_card())
    db.delete_deck(conn, deck_id)
    assert db.get_deck(conn, deck_id) is None
    assert db.get_card(conn, card["id"]) is None


def test_record_review_updates_schedule_and_history(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    card = db.add_card(conn, deck_id, make_card())

    updated = db.record_review(conn, card["id"], quality=4)
    assert updated["repetition"] == 1
    assert updated["interval_days"] == 1
    assert updated["last_reviewed_at"]

    reviews = conn.execute("SELECT * FROM reviews WHERE card_id = ?", (card["id"],)).fetchall()
    assert len(reviews) == 1
    assert reviews[0]["quality"] == 4


def test_record_review_calculates_due_date_from_supplied_time(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    card = db.add_card(conn, deck_id, make_card())
    reviewed_at = datetime(2024, 5, 1, tzinfo=UTC)

    updated = db.record_review(conn, card["id"], quality=4, now=reviewed_at.isoformat())

    assert datetime.fromisoformat(updated["due_at"]) == reviewed_at + timedelta(days=1)


def test_next_due_card_respects_due_date(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    card = db.add_card(conn, deck_id, make_card())

    # a fresh card is due immediately
    assert db.next_due_card(conn, deck_id)["id"] == card["id"]

    # after a good review the interval pushes it into the future
    db.record_review(conn, card["id"], quality=4)
    assert db.next_due_card(conn, deck_id) is None


def test_deck_stats(tmp_db):
    conn = db.connect(tmp_db)
    deck_id = db.create_deck(conn, "Biology")
    c1 = db.add_card(conn, deck_id, make_card())
    db.add_card(conn, deck_id, make_card(front="Q2", back="A2"))
    db.record_review(conn, c1["id"], quality=4)

    stats = db.deck_stats(conn, deck_id)
    assert stats["total"] == 2
    assert stats["new"] == 1
    assert stats["due"] == 1

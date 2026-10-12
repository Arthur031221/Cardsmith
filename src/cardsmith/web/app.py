"""FastAPI application: upload, generate, edit, study, and export decks."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cardsmith import anki, db
from cardsmith import ingest as ingest_mod
from cardsmith.llm.client import ChatClient
from cardsmith.llm.generate import generate_cards
from cardsmith.llm.schema import Card

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_CHUNKS = 60


def create_app(db_path: str, client: ChatClient, cards_per_chunk: int = 4) -> FastAPI:
    app = FastAPI(title="cardsmith", docs_url="/api/docs")
    app.state.db_path = db_path
    app.state.client = client
    app.state.cards_per_chunk = cards_per_chunk

    db.connect(db_path).close()  # make sure the schema exists before serving requests

    def get_conn():
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health():
        ping = getattr(app.state.client, "ping", None)
        ok, message = ping() if callable(ping) else (True, "ok")
        return {
            "ollama_ok": ok,
            "message": message,
            "model": getattr(app.state.client, "model", ""),
        }

    @app.post("/api/generate")
    async def generate(file: UploadFile = File(...), title: str | None = Form(None)):
        name = file.filename or "upload"
        suffix = Path(name).suffix.lower()
        if suffix not in (".pdf", ".pptx", ".txt", ".md"):
            raise HTTPException(
                400, f"unsupported file type '{suffix}'. Use .pdf, .pptx, .txt, or .md"
            )
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, f"file exceeds the {MAX_UPLOAD_BYTES} byte limit")
        if not data:
            raise HTTPException(400, "uploaded file is empty")

        warnings: list[str] = []
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                chunks = ingest_mod.extract_any(tmp_path, warnings=warnings)
            except ingest_mod.UnsupportedFileError as e:
                raise HTTPException(400, str(e)) from e
            except Exception as e:  # pymupdf/pptx raise their own exception types
                raise HTTPException(400, f"could not read {name}: {e}") from e
        finally:
            if tmp_path:
                os.unlink(tmp_path)

        if not chunks:
            detail = f"no extractable text found in {name}"
            if suffix == ".pdf":
                detail += (
                    ". Cardsmith reads PDF text layers and does not perform OCR or interpret "
                    "images. Run OCR for scanned text, or transcribe equations and describe "
                    "diagrams in a text file before generating cards."
                )
            raise HTTPException(400, detail)
        if len(chunks) > MAX_CHUNKS:
            raise HTTPException(
                400,
                f"{name} produced {len(chunks)} chunks, which is more than the {MAX_CHUNKS} "
                "limit for one generation run. Split the file and try again.",
            )

        deck_title = title or Path(name).stem
        cards, errors = generate_cards(
            app.state.client, chunks, deck_title, app.state.cards_per_chunk
        )
        if not cards and errors:
            raise HTTPException(502, f"card generation failed: {errors[0]}")
        return {
            "deck_title": deck_title,
            "source_filename": name,
            "chunk_count": len(chunks),
            "cards": [c.to_dict() for c in cards],
            "errors": errors,
            "warnings": warnings,
        }

    @app.post("/api/decks")
    def create_deck_endpoint(payload: dict, conn=Depends(get_conn)):
        title = (payload.get("title") or "Untitled deck").strip() or "Untitled deck"
        source_filename = payload.get("source_filename", "")
        cards_in = payload.get("cards") or []
        deck_id = db.create_deck(conn, title, source_filename)
        cards = [Card.from_dict(c) for c in cards_in]
        saved = db.add_cards(conn, deck_id, cards) if cards else []
        return {"deck": db.get_deck(conn, deck_id), "cards": saved}

    @app.get("/api/decks")
    def list_decks_endpoint(conn=Depends(get_conn)):
        return {"decks": db.list_decks(conn)}

    @app.get("/api/decks/{deck_id}")
    def get_deck_endpoint(deck_id: int, conn=Depends(get_conn)):
        deck = db.get_deck(conn, deck_id)
        if not deck:
            raise HTTPException(404, "deck not found")
        return {
            "deck": deck,
            "cards": db.list_cards(conn, deck_id),
            "stats": db.deck_stats(conn, deck_id),
        }

    @app.delete("/api/decks/{deck_id}")
    def delete_deck_endpoint(deck_id: int, conn=Depends(get_conn)):
        if not db.get_deck(conn, deck_id):
            raise HTTPException(404, "deck not found")
        db.delete_deck(conn, deck_id)
        return {"ok": True}

    @app.post("/api/decks/{deck_id}/cards")
    def add_card_endpoint(deck_id: int, payload: dict, conn=Depends(get_conn)):
        if not db.get_deck(conn, deck_id):
            raise HTTPException(404, "deck not found")
        card = Card.from_dict(payload)
        return db.add_card(conn, deck_id, card)

    @app.put("/api/decks/{deck_id}/cards/{card_id}")
    def update_card_endpoint(deck_id: int, card_id: int, payload: dict, conn=Depends(get_conn)):
        card = db.get_card(conn, card_id)
        if not card or card["deck_id"] != deck_id:
            raise HTTPException(404, "card not found")
        return db.update_card(conn, card_id, payload)

    @app.delete("/api/decks/{deck_id}/cards/{card_id}")
    def delete_card_endpoint(deck_id: int, card_id: int, conn=Depends(get_conn)):
        card = db.get_card(conn, card_id)
        if not card or card["deck_id"] != deck_id:
            raise HTTPException(404, "card not found")
        db.delete_card(conn, card_id)
        return {"ok": True}

    @app.get("/api/decks/{deck_id}/study/next")
    def study_next_endpoint(deck_id: int, conn=Depends(get_conn)):
        if not db.get_deck(conn, deck_id):
            raise HTTPException(404, "deck not found")
        card = db.next_due_card(conn, deck_id)
        return {"card": card, "stats": db.deck_stats(conn, deck_id)}

    @app.post("/api/decks/{deck_id}/study/{card_id}")
    def study_review_endpoint(deck_id: int, card_id: int, payload: dict, conn=Depends(get_conn)):
        quality = payload.get("quality")
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            quality = None
        if quality is None or not (0 <= quality <= 5):
            raise HTTPException(400, "quality must be an integer 0 to 5")
        card = db.get_card(conn, card_id)
        if not card or card["deck_id"] != deck_id:
            raise HTTPException(404, "card not found")
        updated = db.record_review(conn, card_id, quality)
        return {"card": updated, "stats": db.deck_stats(conn, deck_id)}

    @app.get("/api/decks/{deck_id}/export")
    def export_endpoint(deck_id: int, conn=Depends(get_conn)):
        deck = db.get_deck(conn, deck_id)
        if not deck:
            raise HTTPException(404, "deck not found")
        cards = db.list_cards(conn, deck_id)
        if not cards:
            raise HTTPException(400, "deck has no cards to export")
        with tempfile.NamedTemporaryFile(suffix=".apkg", delete=False) as tmp:
            out_path = tmp.name
        anki.export_apkg(deck["title"], str(deck_id), cards, out_path)
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", deck["title"]).strip("_") or "deck"
        return FileResponse(
            out_path, media_type="application/octet-stream", filename=f"{filename}.apkg"
        )

    return app

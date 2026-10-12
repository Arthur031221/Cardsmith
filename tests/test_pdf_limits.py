"""Exercise PDF limitations through real PDFs and the shipped upload endpoint."""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from cardsmith.web.app import create_app

from .conftest import FakeChatClient, card_response

PASSAGE = "The nucleus stores genetic material."
EQUATION = "E = m c^2"


def write_pdf(tmp_path, pages):
    path = tmp_path / "notes.pdf"
    with pymupdf.open() as doc:
        for text, visual in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
            if visual == "image":
                # Rasterize an equation and a diagram, leaving no PDF text layer.
                with pymupdf.open() as source:
                    source_page = source.new_page(width=200, height=100)
                    source_page.insert_text((20, 30), EQUATION)
                    source_page.draw_circle((100, 70), 15)
                    image = source_page.get_pixmap().tobytes("png")
                page.insert_image(pymupdf.Rect(72, 100, 272, 200), stream=image)
            elif visual == "drawing":
                page.draw_circle((100, 150), 20)
                page.draw_line((120, 150), (180, 150))
        doc.save(path)
    return path


def upload_pdf(tmp_db, path, chat=None):
    chat = chat or FakeChatClient(responses=[card_response(PASSAGE)])
    client = TestClient(create_app(db_path=tmp_db, client=chat))
    response = client.post(
        "/api/generate", files={"file": (path.name, path.read_bytes(), "application/pdf")}
    )
    return response, chat


def test_image_only_pdf_gets_actionable_error(tmp_db, tmp_path):
    path = write_pdf(tmp_path, [("", "image")])
    response, chat = upload_pdf(tmp_db, path, FakeChatClient())
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "no extractable text" in detail
    assert "OCR" in detail
    assert "equations" in detail and "diagrams" in detail
    assert chat.calls == []


def test_mixed_pdf_reports_skipped_page(tmp_db, tmp_path):
    path = write_pdf(tmp_path, [("", "image"), (PASSAGE, None)])
    response, chat = upload_pdf(tmp_db, path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["warnings"]) == 1
    assert "page 1" in body["warnings"][0]
    assert "skipped" in body["warnings"][0]
    assert "OCR" in body["warnings"][0]
    assert body["cards"][0]["source_location"] == "page 2"
    assert body["cards"][0]["grounded"] is True
    assert len(chat.calls) == 1
    assert EQUATION not in chat.calls[0][0][1]["content"]


@pytest.mark.parametrize("visual", ["image", "drawing"])
def test_text_pdf_reports_unread_visuals(tmp_db, tmp_path, visual):
    path = write_pdf(tmp_path, [(PASSAGE, visual)])
    response, chat = upload_pdf(tmp_db, path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["warnings"]) == 1
    warning = body["warnings"][0]
    assert "page 1" in warning
    assert "images or vector graphics" in warning
    assert "equations" in warning and "diagrams" in warning
    assert "extracted text only" in warning
    assert body["cards"][0]["grounded"] is True
    assert body["errors"] == []
    assert len(chat.calls) == 1
    assert EQUATION not in chat.calls[0][0][1]["content"]


def test_text_equation_is_sent_to_model(tmp_db, tmp_path):
    path = write_pdf(tmp_path, [(f"{PASSAGE}\n{EQUATION}", None)])
    response, chat = upload_pdf(tmp_db, path)
    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == []
    assert EQUATION in chat.calls[0][0][1]["content"]


def test_unused_pdf_image_resource_is_not_reported(tmp_db, tmp_path):
    path = write_pdf(tmp_path, [(PASSAGE, "image")])
    with pymupdf.open(path) as doc:
        page = doc[0]
        # Keep the image resource but remove the operation that displays it.
        doc.update_stream(page.get_contents()[-1], b"")
        data = doc.tobytes()
    path.write_bytes(data)
    with pymupdf.open(path) as doc:
        assert doc[0].get_images()
        assert doc[0].get_image_info() == []
    response, _ = upload_pdf(tmp_db, path)
    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == []

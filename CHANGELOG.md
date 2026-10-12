# Changelog

## Unreleased

- Warn in the generation preview when PDF pages have no extractable text or contain
  images or vector graphics that are not interpreted. Return these notices in the
  API's `warnings` array, give image-only uploads OCR and transcription guidance,
  and explain the limits of PDF equation and diagram extraction.
- Limit upload reads to one byte over the configured maximum before rejecting oversized files.
- Fix the Ollama connection check accepting a different tag from the configured model.
- Hand-rated the 40-card generation benchmark for factual accuracy: 37 of 40
  accurate (92.5 percent). Results and per-card ratings are in
  `eval/results.md` and `eval/rated_cards.md`.

## 0.1.0

Initial release.

- Ingest PDF (pymupdf), PowerPoint (python-pptx), and plain text or Markdown files.
- Generate basic and cloze flashcards with a local Ollama model (qwen3:4b by
  default), through the native `/api/chat` endpoint with `think` disabled
  and a strict JSON schema for the response.
- Every generated card carries a source quote and its location (page or
  slide), and cardsmith flags cards whose quote cannot be matched verbatim
  in the source chunk.
- Local web UI (FastAPI, single page) with an editable preview before cards
  are saved.
- SM-2 spaced repetition scheduler backed by SQLite.
- Export any deck to an Anki `.apkg` package through genanki, with stable
  deck and note model ids so re-exporting the same deck updates it in Anki
  instead of duplicating it.
- `uvx cardsmith` starts the server and opens a browser tab.

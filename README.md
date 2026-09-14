# AI Product Catalog Agent

A practical AI-assisted catalog generation system for supplement and product businesses.

The application takes one or more product photos per SKU, extracts visible product information, normalizes and classifies the data, creates a studio-style catalog image, supports human review and price entry, and generates a branded PDF catalog.

## Project status

**Architecture v1.0 frozen. Initial repository scaffold created.**

Implementation is intentionally incremental: the final product architecture is defined up front, while each block is built and smoke-tested without disposable prototype rewrites.

## Core workflow

```text
Photos
  ↓
Intake + deduplication
  ↓
Vision extraction
  ↓
Normalization + classification
  ↓
AI image editing
  ↓
Human review + pricing
  ↓
Approval
  ↓
Catalog snapshot
  ↓
HTML/CSS rendering
  ↓
PDF catalog
```

## Architecture principles

- Deterministic workflow orchestration; no LLM is used to decide obvious pipeline transitions.
- AI is used where interpretation or visual transformation is genuinely useful.
- Human-entered values override model-generated values.
- Prices are never invented by the model.
- Original images are always preserved.
- Edited catalog images are separate assets and require review before publication.
- Multiple photos can belong to a single SKU.
- Catalog versions are immutable snapshots so historical PDFs remain reproducible.
- Expensive operations are queued as durable jobs with idempotency/caching support.
- The initial product is local and single-user; infrastructure stays intentionally simple.

## Planned stack

### Backend
- Python 3.12+
- FastAPI
- SQLAlchemy
- SQLite
- Alembic
- Pydantic

### Frontend
- React
- TypeScript
- Vite

### AI
- Multimodal model for structured product extraction
- Image editing model for high-fidelity studio-style product presentation
- Optional OCR fallback for uncertain fields

### Catalog rendering
- HTML/CSS templates
- WeasyPrint-compatible PDF rendering layer

## Repository structure

```text
ai-product-catalog-agent/
├── AGENTS.md
├── README.md
├── docs/
│   ├── architecture-v1.md
│   └── decisions.md
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── core/
│   │   ├── db/
│   │   ├── domain/
│   │   ├── services/
│   │   ├── workers/
│   │   └── ai/prompts/
│   └── tests/
├── frontend/
├── skills/
│   ├── product-extraction/
│   └── catalog-image/
├── templates/grabelan/
└── storage/
```

## Important distinction: Codex Skills vs runtime AI prompts

`skills/` contains reusable instructions for Codex/agent workflows while working on this repository.

`backend/app/ai/prompts/` will contain the prompts and schemas used by the actual catalog application at runtime.

They are related, but they are **not the same thing**.

## Current milestone

The next implementation block is the **domain contract**:

1. Brand / Product / SKU / Photo relationships
2. per-step processing state
3. field provenance and human locks
4. price history
5. job queue records
6. catalog snapshot model

No production credentials, real customer data, or private catalog assets should be committed to this repository.

## Manual OpenAI extraction smoke test

Automated tests use fake providers and never call OpenAI. To make one explicit
development call, first install the backend and migrate the configured database:

```powershell
cd backend
python -m pip install -e ".[dev]"
python -m alembic upgrade head
$env:OPENAI_API_KEY = "your-key-from-your-secret-store"
$env:OPENAI_VISION_MODEL = "gpt-5.6-sol"
$env:DATABASE_URL = "sqlite:///./catalog.db"
python -m app.scripts.manual_openai_extraction --photo-id <REGISTERED_PHOTO_UUID>
```

Repeat `--photo-id` for additional registered original photos. The command
enqueues or reuses the deterministic `product.extract.v1` Job, performs at most
one provider attempt, and prints only the validated result, normalized usage and
sanitized status. It does not print credentials, image data or raw responses.
After correcting an implementation or configuration problem, an existing failed
Job can be retried explicitly while its attempt budget remains:

```powershell
python -m app.scripts.manual_openai_extraction --photo-id <REGISTERED_PHOTO_UUID> --requeue-failed
```

This reuses the same idempotent Job and does not reset its attempt counter.

## Manual OpenAI image-enhancement smoke test

After migrating to head, use a registered original Photo with the durable image
enhancement pipeline:

```powershell
cd backend
$env:OPENAI_API_KEY = "your-key-from-your-secret-store"
$env:OPENAI_IMAGE_MODEL = "gpt-image-2.5-sunburst"
$env:DATABASE_URL = "sqlite:///./catalog.db"
python -m app.scripts.manual_openai_image_enhancement --photo-id <PHOTO_UUID>
```

The command reports safe Job, run and DerivedImage metadata. It never prints
credentials or generated base64. Use `--requeue-failed` only for an existing
failed logical Job that still has attempt budget; normal idempotency and the
attempt counter are preserved.

# AI Product Catalog Agent

A practical AI-assisted catalog generation system for supplement and product businesses.

The application takes one or more product photos per SKU, extracts visible product information, normalizes and classifies the data, creates a studio-style catalog image, supports human review and price entry, and generates a branded PDF catalog.

## Local Quick Start (Windows PowerShell 5.1+)

One-time setup: install Python 3.12+, Node.js/npm and the project dependencies. From the repository root:

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
cd ..\frontend
npm ci
cd ..
```

For daily use, run `.\doctor.ps1`, then `.\start.ps1` and open
`http://127.0.0.1:5174`. Check services with `.\status.ps1`; when finished,
run `.\stop.ps1`. Startup upgrades the configured SQLite database to Alembic
head before starting services. Doctor and status do not migrate it. Logs live in
`.runtime/logs/<run-id>/`; `.runtime/` is ignored by Git.

Defaults are backend `127.0.0.1:8001` and frontend `127.0.0.1:5174`.
If either is occupied, the launcher refuses to start and **never stops the
occupant**. Choose explicit ports, for example
`.\start.ps1 -BackendPort 8002 -FrontendPort 5184`, and pass the same ports to
`doctor.ps1`; Vite's API proxy follows the selected backend port automatically.
The launcher binds loopback only and Vite uses a strict frontend port.

`OPENAI_API_KEY` is optional for deterministic workflows. Configure it in your
Windows Current User environment (or the launching PowerShell session) and
restart the shell before starting to enable Product extraction, Product Copy
and image enhancement workers. Without it, those three workers are skipped;
manual Product/Category management and catalog rendering remain available.
The key is never stored in runtime state or printed. An optional `DATABASE_URL`
must point to a file-backed SQLite database; otherwise all Python processes and
Alembic use `backend/catalog.db`. Relative SQLite URLs resolve from `backend`.
Neither startup nor doctor installs dependencies or creates sample Products.

For troubleshooting, use `doctor.ps1`, inspect the run-scoped logs, stop any
partial launcher-owned runtime with `stop.ps1`, and retry. The individual
commands below remain available for debugging, not ordinary daily operation.

Process ownership is anchored to the launched PID, exact creation time,
requested executable and command marker. Windows can briefly omit `Process.Path`
at launch; workers retry incomplete metadata within their two-second startup
window (no fixed startup delay). A missing PID fails immediately; a proven
identity mismatch is never accepted. The venv Python parent stays tracked;
shutdown verifies and stops its descendants, without searching for other Python
workers. If metadata cannot be verified, status shows `Unverified` and the state
is retained for a later safe stop, including failed-start rollback.

Focused launcher checks (PowerShell 5.1 or 7, no app/database/provider calls):
`powershell.exe -NoProfile -File scripts/dev/tests/launcher-tests.ps1`.
An optional isolated venv diagnostic is
`powershell.exe -NoProfile -File scripts/dev/tests/inspect-worker-startup.ps1`;
it only launches short-lived Python wait processes, prints ownership predicates
and child metadata, and stops its verified test trees.

## Catalog History

Open `/catalogs` from the application navigation to browse Catalog Builds,
including queued and failed ones. Search frozen Publisher and Cover/edition
text, filter by status, Publisher, Theme or creation date, and open a Build to
inspect its frozen products, prices, presentation and render attempts. A valid
PDF can be previewed or downloaded through the existing Builder artifact
endpoint. Historical Builds are read-only; the Library never changes their
configuration, content or PDF.

From a valid History detail, **Duplicate as new draft** opens the existing
Builder with previously selected Products and presentation settings as a
starting point. It does not create a Build. Ready Products are selected using
current data; Products that are no longer ready or available appear under
Needs attention. The next explicit **Create catalog** action produces a new
Snapshot from current Product names, categories, approved prices, copy, media,
publishable SKUs and Publisher branding. The old Snapshot and PDF remain
unchanged. Failed or running Builds can also be duplicated when their frozen
configuration and Snapshot are valid; a PDF is not required.

The draft is not saved. Refreshing a `?duplicateFrom=` URL reloads the source
template and discards unsaved edits. **Start fresh** removes that source and
resets the Builder. If both `build` and `duplicateFrom` are present, `build`
takes precedence. Old custom palettes and Publisher contact overrides that
predate choice-provenance metadata need an explicit review in the Builder;
resolved historical values are not silently treated as original choices.

Manual 14B smoke using an existing v5 catalog (this creates a real new Build
only at step 9):

1. Run `.\start.ps1 -FrontendPort 5184` and open `/catalogs`.
2. Open a v5 catalog; record its Build ID, Product count, Layout, Theme,
   Cover and Closing.
3. Click **Duplicate as new draft**. Confirm the source banner and that
   currently Ready Products are selected.
4. Confirm History count has not changed and current Product cards show current
   values, not frozen prices.
5. Review Needs attention, Layout, Theme, Palette, Cover, Hero and Closing.
6. Edit a title, edition or Theme to confirm the draft is editable.
7. If any required choice is unresolved, select it explicitly.
8. Confirm the intended current Product/Publisher choices before publishing.
9. Click **Create catalog** once; the URL should change to `?build=<new-id>`.
10. Open the new History detail, verify its source link and current pricing,
    preview its PDF, and confirm the old Build is unchanged.
11. Run `.\stop.ps1`.

Optionally open a v2 History record and click Duplicate without creating a
Build. Theme should be a marked current default; Cover and Closing should
start disabled. Click **Start fresh** afterward.

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

The local Product, Category and Catalog workspaces are implemented. The
one-command launcher provides repeatable local startup without changing the
frozen Architecture v1.0 or adding a hosted service.

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

## Manual catalog PDF render smoke test

Install the backend dependencies and the local Chromium binary once:

```powershell
cd backend
python -m pip install -e ".[dev]"
python -m playwright install chromium
python -m alembic upgrade head
```

Then render an existing immutable CatalogSnapshot through the durable pipeline:

```powershell
$env:DATABASE_URL = "sqlite:///./catalog.db"
python -m app.scripts.manual_catalog_render --snapshot-id <CATALOG_SNAPSHOT_UUID>
```

The command prints only Job, render-run and PDF-artifact metadata. It does not
persist or print self-contained HTML or embedded image data. Use
`--requeue-failed` only when the same failed logical Job still has attempt
budget.

## Catalog Builder end-to-end smoke (Block 10C)

From `backend`, use the existing local `catalog.db` for both API and worker.
Install Chromium once if needed. The migration changes the real local database,
so run these commands only when you are ready for the manual smoke:

```powershell
cd backend
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

In a second terminal, from `backend`, start the existing durable JobWorker
(supports `catalog.render.v2`, v3 and v4):

```powershell
cd backend
$env:DATABASE_URL = "sqlite:///./catalog.db"
.\.venv\Scripts\python.exe -m app.scripts.run_catalog_render_worker
```

In a third terminal, start React:

```powershell
cd frontend
npm run dev -- --port 5174
```

Open `http://127.0.0.1:5174/catalog-builder`. Select **LANDERFIT Premium
Whey** (`fd70be92-d0fe-46a7-9533-dbe50ecf6af5`), choose **Grabelan** and
**Classic**, and click **Create catalog** once. Watch the queued/running state
until **Catalog ready**, then preview and download the PDF. Visually confirm
Grabelan publisher branding, LANDERFIT as Product Brand, the correct Product
name and SKU, current approved/human copy, active PYG Price, and the selected
approved enhanced image. Change the Builder control to Dense afterward; the
already generated Classic result must still say Classic. This is a manual smoke;
the test suite does not create a real LANDERFIT build.

## Optional catalog cover and edition (Block 12B)

The Builder can add exactly one front cover using the versioned Minimal,
Editorial or Hero composition. Edition title, subtitle and label are explicit
user-entered plain text. Hero requires an uploaded image; Minimal and Editorial
also work without one. Cover choices are frozen in `catalog.render.v4` and do
not change Product pages, the publisher profile or historical v2/v3 artifacts.

Hero uploads accept decoded PNG, JPEG or WEBP up to **10 MiB and 24 million
pixels**. The upload MIME type must match the decoded content. Bytes are kept
immutably by checksum under local `storage/covers`; the API exposes only an
asset ID and a verified image endpoint. Removing a Hero from the Builder only
removes its reference for the next Build. An upload never used by a Build may
remain as an unreferenced asset; 12B has no garbage collector.

After upgrading the local database with `python -m alembic upgrade head`, use
the same Catalog render worker command above. It now accepts v2, v3 and v4
Jobs. Frontend development remains on port 5174. The synthetic developer-only
gallery can be regenerated with `python -m app.scripts.render_cover_gallery`
from `backend`; it writes ignored PDFs under `tmp/pdfs` and uses no real Builds.

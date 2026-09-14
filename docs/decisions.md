# Architecture Decision Log

## ADR-001 — Build one final-product architecture in blocks
**Decision:** Freeze the target architecture first, then implement it incrementally.

**Why:** The user/problem is already known. Disposable MVP architectures would create unnecessary rewrite work.

---

## ADR-002 — Deterministic orchestration
**Decision:** Pipeline coordination uses normal code, not an LLM.

**Why:** Known dependencies and state transitions do not require reasoning and should remain reproducible and cheap.

---

## ADR-003 — Agentic behavior only for uncertainty recovery
**Decision:** Reserve agentic decision-making for ambiguous visual/extraction recovery.

**Why:** This is where observation → decision → action → reevaluation can provide real value.

---

## ADR-004 — React + FastAPI
**Decision:** Use React/TypeScript frontend and FastAPI backend.

**Why:** The system is intended to become a polished usable portfolio application, while the backend remains independently testable.

---

## ADR-005 — SQLite + local filesystem initially
**Decision:** No remote database or cloud asset store initially.

**Why:** Local single-user usage does not justify distributed infrastructure.

---

## ADR-006 — Durable SQLite-backed job queue
**Decision:** Expensive AI work executes in a background worker and is persisted as jobs.

**Why:** Prevent HTTP timeouts, allow retries, and avoid losing batch progress.

---

## ADR-007 — Product / SKU / Photo separation
**Decision:** A Product is conceptual, a SKU is sellable, and multiple Photos may belong to a SKU.

**Why:** Price and variants belong to SKU, while extraction may require multiple views.

---

## ADR-008 — Human precedence and field provenance
**Decision:** Store source/confidence/evidence/status/lock metadata for important extracted fields.

**Why:** Reprocessing must never silently overwrite reviewed human corrections.

---

## ADR-009 — AI image editing is allowed
**Decision:** Use high-fidelity generative image editing, preserving originals and requiring approval.

**Why:** Studio-style visual improvement is a core benefit. Pixel identity is not required, but meaningful packaging discrepancies must not be silently published.

---

## ADR-010 — Multimodal-first extraction, OCR fallback
**Decision:** Do not run mandatory double OCR/model processing for every product.

**Why:** Start with structured multimodal extraction. Add targeted OCR/retry when confidence or evidence is insufficient.

---

## ADR-011 — Immutable catalog snapshots
**Decision:** Each generated catalog version freezes product data, price and selected assets.

**Why:** Historical catalogs must remain reproducible even when live data changes.

---

## ADR-012 — Original photos may precede SKU identification
**Decision:** Permit an original Photo to be registered with no SKU association. Link it to a real SKU only after identification; do not create placeholder Products or SKUs. Intake metadata is mandatory for new uploads but remains nullable in persistence so pre-intake Photo rows can migrate without fabricated metadata.

**Why:** The intake workflow must preserve uploaded bytes before AI or a human has identified the commercial entity. Content-derived storage identity keeps that preservation independent of filenames and later classification.

---

## ADR-013 — Single-worker stale-job recovery is at-least-once
**Decision:** A single SQLite-backed worker commits a Job as running before invoking its handler. On startup or explicit recovery, running jobs older than a configured timeout return to the queue when attempts remain.

**Why:** No database transaction should remain open during slow external work. A crash can therefore occur after an external side effect but before success is recorded, so stale recovery may execute a handler again. Future handlers must use the Job idempotency key and operation-specific idempotency safeguards.

---

## ADR-014 — Extraction runs are observational records
**Decision:** Each ExtractionRun records one concrete model attempt and its input Photos, configuration, validated result or sanitized error. Completing a run does not update canonical Product, SKU or field-provenance state, and terminal runs cannot be overwritten.

**Why:** Provider output must remain auditable and distinct from reviewed canonical data. A separate application step can later enforce normalization, human locks and approval rules.

---

## ADR-015 — OpenAI vision stays behind a narrow provider boundary
**Decision:** `product.extract.v1` uses an injectable vision-provider interface. The first adapter calls the OpenAI Responses API with locally read image data URLs, `gpt-5.6-sol`, low reasoning effort and Structured Outputs derived from the existing Pydantic result schema. Caller photo order is incidental; verified assets are sent in checksum order. Complete raw responses are not retained.

**Why:** Provider SDK concerns, credentials and transient error mapping should remain outside deterministic extraction orchestration. Canonical ordering aligns execution with idempotency, while one ExtractionRun per provider call preserves retry auditability without storing image bytes or raw responses.

---

## ADR-016 — Human review is an immutable bridge to canonical SKU state
**Decision:** A field review records one accepted, corrected or rejected human decision for one successful ExtractionRun, target SKU and SKU field bundle. Accepted and corrected decisions atomically update canonical SKU fields plus locked provenance; rejected decisions are audit-only. `applied_at` means canonical mutation completed, so it remains null for rejected reviews. Size value and unit share one review decision and transaction.

**Why:** Extraction runs must remain immutable observations while canonical state changes only through an explicit human action. Separate review audit, canonical value and provenance records preserve both model lineage and human precedence without introducing another source of truth.

---

## ADR-017 — Identity-key v1 and human-only identity resolution
**Decision:** Brand and Product exact identity keys use Unicode NFKC normalization, trimmed/collapsed whitespace and casefolding. Brand keys are globally unique; Product keys are unique within a Brand. Loose keys may ignore whitespace and punctuation only to suggest candidates. A typed human decision must explicitly use an existing entity or create a named new one; suggestions never select, merge or create entities.

**Why:** Stable exact keys provide deterministic database integrity without confusing near matches with identity. Versioning the algorithm ensures future normalization changes require an explicit migration and collision review.

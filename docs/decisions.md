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

---

## ADR-018 — SKU variants use conservative comparison and exclusive Photo ownership
**Decision:** SKU remains the commercial variant and receives no permanent identity key. Manual creation rejects only an exact normalized dimension tuple within one Product; partial and external-SKU matches are suggestions. A Photo may be unassigned, Product-owned or SKU-owned, never both. Effective SKU media uses matching SKU photos when present and otherwise falls back to matching Product photos without combining levels.

**Why:** Variant dimensions may be incomplete and differ across product types, while units cannot yet be safely converted. Exclusive ownership preserves clear asset meaning, and deterministic fallback lets variants share media without duplicating immutable image bytes.

---

## ADR-019 — Configurable flat Product taxonomy with one optional primary
**Decision:** Category is configurable persisted data with identity-key v1, not an enum. ProductCategory is the canonical metadata-bearing many-to-many association. A Product may have zero or more associations and at most one primary, enforced with a SQLite partial unique index. Human replacement demotes the previous primary without deleting it; inactive Categories retain historical associations but reject new assignments. Removing a missing association fails explicitly rather than silently succeeding.

**Why:** Products, rather than SKU variants, define catalog grouping. A flat configurable taxonomy supports different businesses and later reviewed AI suggestions without hardcoded vertical assumptions or duplicate canonical state.

---

## ADR-020 — Category suggestions are immutable advisory bundles
**Decision:** A versioned durable job snapshots canonical Product, Brand and SKU context plus the active taxonomy before requesting a structured category suggestion. Every provider attempt creates an immutable CategorySuggestionRun and never mutates ProductCategory. One explicit human bundle review accepts, corrects or rejects the run. Acceptance recomputes the original logical input hash from the current trusted snapshot and the run's persisted provider/model/prompt/schema/parameter lineage, and rejects a stale run before creating audit or canonical rows. Corrected stale runs remain valid explicit human choices after current-category validation; rejected stale runs remain audit-only. Accepted new associations retain model/run lineage; corrected new associations are human-sourced without model lineage; existing association origins are never rewritten. Rejected reviews have `applied_at=null`.

**Why:** Taxonomy and Product context can change after inference. Persisted inputs make suggestions reproducible, while a separate atomic human-review boundary prevents confidence-based auto-classification and preserves canonical provenance.

---

## ADR-021 — Catalog readiness is derived from live canonical state
**Decision:** Product catalog readiness is a read-only diagnostic for an explicit currency and UTC point in time. It requires an active canonical primary Category, at least one SKU, at least one SKU with the deterministic active approved Price, and one available front Photo selected by Product-first then stable SKU fallback ordering. Inactive secondary Categories and excluded unpriced SKUs are warnings. AI workflow history is not consulted, and no readiness value is persisted.

**Why:** Category activation, prices, media ownership and local asset availability can change independently. Deriving readiness prevents stale flags while producing the exact Product, SKU, Price and Photo references that a later immutable CatalogSnapshot can freeze.

---

## ADR-022 — Image enhancement creates unapproved immutable derived assets
**Decision:** `image.enhance.v1` accepts only an original Photo, verifies its exact bytes before a provider call, and records every provider attempt as a separate ImageEnhancementRun. Successful output is validated and atomically stored by output checksum under `storage/processed`, then represented by a distinct DerivedImage linked to the run and source Photo. DerivedImage inherits Product/SKU meaning through that Photo and carries no duplicate owner or approval/preference state. A safely stored file left without a database row after completion failure is a recoverable orphan and is not deleted automatically.

**Why:** Originals are immutable source evidence, while generative edits require exact audit lineage and later human review. Separating processed assets prevents successful generation from silently changing catalog presentation, and retaining content-addressed orphans avoids deleting bytes that another deduplicated audit row may reference.

---

## ADR-023 — Image approval history and presentation preference are separate
**Decision:** Human approval is derived from append-only DerivedImageReview events ordered by creation time and UUID. Current presentation is a singular mutable PhotoPresentationPreference: it may explicitly select one currently approved DerivedImage belonging to the same source Photo, or select the original with a null reference. Rejection of the selected image clears preference in the same caller-owned transaction. Readiness retains its source-hero algorithm and resolves presentation afterward; unavailable preferred derived files fall back to the original with a warning and no read-time repair.

**Why:** Approval is auditable evidence, while presentation preference is revisable current state. Keeping them distinct prevents generation or approval alone from changing a catalog, preserves exact source lineage, and lets a later immutable CatalogSnapshot freeze both source and presentation identities.

---

## ADR-024 — Catalog snapshots are hashed historical value data
**Decision:** An explicit snapshot request evaluates every selected Product at one shared currency and UTC point in time, includes all publishable SKUs by default or an exact validated subset, and freezes deterministic Category sections plus Brand/Product/SKU/Price and source/effective hero values in a strict JSON payload. Source UUIDs are traceability values rather than foreign keys. Decimal values serialize as normalized strings; portable asset paths are relative to the canonical content-addressed storage root; selected source and presentation bytes receive full integrity validation. The complete canonical JSON payload is hashed, but the hash is not unique and the create service does not commit. Historical reads validate schema, row metadata and hash without consulting live state.

**Why:** Rendering from live rows would let later names, taxonomy, prices or image preferences rewrite publication history. Value snapshots preserve reproducibility while reusing immutable original/processed assets. External deletion of those content-addressed files remains an operational threat to re-renderability; per-snapshot asset copying and garbage collection are intentionally deferred.

---

## ADR-025 — Catalog rendering is an audited offline transformation
**Decision:** `catalog.render.v1` consumes only a validated CatalogSnapshot and its frozen presentation assets. A versioned local template is identified by key, human version and a deterministic hash of Jinja/CSS/static bytes. Normalized locale/page configuration, template lineage, renderer version, snapshot UUID and snapshot content identity form the durable Job key. Each browser attempt has an immutable CatalogRenderRun committed before Chromium starts. Successful PDF bytes are validated with pypdf, stored atomically by checksum under `storage/catalogs`, then linked through one immutable CatalogArtifact in a fresh transaction. Render HTML is transient, autoescaped, data-URI self-contained and executed in an offline browser context.

**Why:** Separating snapshot content, prepared presentation and browser execution prevents live commerce changes from rewriting history and prevents long SQLite transactions during Chromium work. Snapshot UUID participates in idempotency to preserve distinct historical audit lineage; exact PDF bytes, rather than assumed browser reproducibility, are the immutable published artifact.

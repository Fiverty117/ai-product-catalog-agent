# Architecture v1.0 — AI Product Catalog Agent

Status: **FROZEN FOR INITIAL IMPLEMENTATION**

## 1. Product goal

A local-first application for converting one or more photos of supplement/product SKUs into a structured, reviewed and priced catalog, including AI-assisted studio-style product imagery and branded PDF export.

The system is designed as a real usable product first and a portfolio project second.

## 2. System boundary

### In scope
- upload one or more photos per SKU
- image storage and deduplication
- visual product data extraction
- structured field provenance
- normalization
- product classification
- high-fidelity AI image editing
- side-by-side human review
- manual price entry
- future price import capability
- approval workflow
- catalog templates
- immutable catalog version snapshots
- PDF generation
- durable background jobs
- lightweight cost/usage metadata

### Out of scope for v1 architecture
- multi-user auth
- payments
- SaaS hosting
- enterprise queues
- cloud object storage
- microservices
- autonomous multi-agent teams

## 3. High-level architecture

```text
React UI
   │
   ▼
FastAPI
   │
   ├────────────── Product / Review API
   │
   └────────────── Job creation
                       │
                       ▼
                  SQLite jobs
                       │
                       ▼
                    Worker
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
   Vision Extract  Classification  Image Edit
          │            │            │
          └────────────┼────────────┘
                       ▼
                 Normalization
                       │
                       ▼
                   Review UI
                       │
              Metadata + Pricing
                       │
                       ▼
                    Approval
                       │
                       ▼
               Catalog Snapshot
                       │
                       ▼
                HTML/CSS Renderer
                       │
                       ▼
                      PDF
```

## 4. Deterministic vs AI boundary

### Deterministic
- orchestrating pipeline steps
- deciding which known dependency is missing
- job scheduling/retry bookkeeping
- idempotency and caching
- validation of structured output
- unit normalization
- database persistence
- price handling
- human override precedence
- approval rules
- snapshot creation
- catalog layout and PDF generation

### AI-assisted
- reading visual information from product photos
- extracting structured fields from visible packaging
- suggesting a category when deterministic rules are insufficient
- editing product photos into studio-style catalog assets

### Agentic behavior
The workflow engine itself is not an LLM agent.

The legitimate agentic extension point is **uncertainty recovery**. A later recovery policy may observe a weak extraction and choose among actions such as:
- re-crop a label region
- retry with another photo
- invoke OCR fallback
- request human review

Initial recovery should use explicit rules. An LLM policy is only introduced if rules prove insufficient.

## 5. Domain model

### Brand
Manufacturer/brand identity.

### Product
The conceptual product line, e.g. "Gold Standard 100% Whey".

### Category
A configurable, flat taxonomy entry used to organize Products. Products may
have multiple Category associations, but at most one is primary for catalog
section placement. Category values are stored data rather than hardcoded enums;
SKU variants inherit their Product's categorization conceptually.

Category suggestions use canonical Product, Brand and SKU context plus a
persisted snapshot of the active taxonomy. Each provider attempt is immutable
and advisory. Only an explicit bundle-level human review may apply selected
Categories to canonical ProductCategory associations. Acceptance first rebuilds
the same trusted snapshot and logical input hash; a stale run may only be
corrected explicitly or rejected, never accepted as model-derived truth.

### SKU
The sellable variant. Price belongs here.

Typical variant dimensions:
- flavor
- presentation/weight
- serving count
- format

### Photo
A photo may be ingested before its SKU is identified. Its SKU association is
therefore optional during intake and may be assigned later. Once identified,
one SKU may have N photos. Unidentified photos do not cause placeholder Product
or SKU records to be created.

A Photo may instead belong to a Product when the same asset represents all of
its SKU variants. Product and SKU ownership are mutually exclusive. SKU-specific
photos override Product-level shared photos for the same requested role; the two
levels are not combined by default.

Optional role:
- front
- side
- back
- nutrition
- other

### ExtractionRun
Stores one concrete, observational AI extraction attempt. It is not canonical
Product/SKU state and does not apply values to field provenance. It includes:
- provider and model
- prompt version
- schema and parameter versions
- one or more input Photos
- validated structured result or sanitized error
- usage/cost metadata
- execution timestamps

The initial provider adapter uses the OpenAI Responses API with image inputs and
schema-constrained output derived from the strict Pydantic extraction contract.
The deterministic worker resolves and verifies original assets, closes the
database transaction, and then invokes the provider. Every provider call creates
a distinct run, including retries of the same durable Job.

### ImageEnhancementRun / DerivedImage
An ImageEnhancementRun records one actual provider edit attempt against one
immutable original Photo. The worker commits the running attempt before reading
and verifying source bytes or calling the provider. A retry creates another run;
terminal attempts are not rewritten.

A successful attempt creates one DerivedImage. DerivedImage is a separate,
immutable processed asset linked to both the exact run and its source Photo. It
does not duplicate Product or SKU ownership: presentation ownership is inherited
through the source Photo. Generated bytes and raw provider responses are not
stored on the run.

### FieldValue / provenance
Important extracted fields preserve:
- value
- source: model | human | rule | ocr
- confidence
- evidence
- field status
- locked flag
- run reference

Field status distinguishes:
- pending
- extracted
- not_legible
- not_present
- failed

Human-locked values always have precedence over re-extraction.

### Price
Price is separate from product metadata and belongs to SKU.

Fields include:
- amount
- currency
- source
- approval state
- valid_from
- created_at

### Job
Durable background work record for external/expensive operations.

Key ideas:
- status
- attempts
- retry time
- idempotency key
- step version

### Catalog
Logical catalog definition.

### CatalogSnapshot
Create-only historical value data containing the exact selected Products/SKUs,
display names, primary Category grouping, active approved Prices and effective
hero presentation. Source UUIDs provide audit traceability inside the JSON
payload but are not foreign keys and are never dereferenced on historical read.
Decimal values use canonical strings and the complete renderable payload has a
deterministic SHA-256 content hash. Equal content may have multiple snapshot
rows; the row UUID identifies the publication event.

A future rendered catalog artifact will reference one CatalogSnapshot and add
template/render/PDF lifecycle data without making the content snapshot mutable.

## 6. Processing state

No single linear product status is authoritative.

Each major step has independent state:
- extraction
- classification
- image_edit
- pricing
- review

Typical step states:
- pending
- running
- ok
- failed
- needs_review

`catalog_ready` is derived, not manually persisted as the single source of truth.

Example rule:

```text
catalog_ready =
  active primary Category exists
  AND at least one SKU exists
  AND at least one SKU has an active approved Price for the requested currency/as_of
  AND a usable front Photo exists at Product level or on one of its SKUs
```

Readiness is a live, deterministic diagnostic over canonical state. AI runs and
review history are not prerequisites. Unpublishable sibling SKUs and inactive
secondary Categories remain visible as non-blocking diagnostics. Catalog
versions later freeze the selected SKU, Price, Category and Photo references;
readiness itself remains unpersisted.

Snapshot creation is the stricter publication boundary. It evaluates every
explicitly selected Product with one shared currency/as-of decision, rejects the
whole request if any Product is not ready, verifies readiness's exact Price and
hero decisions, then fully validates only the original and effective hero
assets being frozen. A missing or invalid preferred derived selection is not
silently published from its readiness fallback; it must be corrected before a
snapshot is created.

## 7. Image policy

The system intentionally uses AI image editing because high-quality catalog presentation is a core product feature.

Rules:
1. Original photo is immutable and always preserved.
2. Edited image is stored as a separate derived asset.
3. Editing prompts explicitly request preservation of packaging identity, label, logo, proportions and visible text.
4. UI shows original and edited assets side by side before approval.
5. Automated comparison may assist review, but it is not a mandatory pixel-identity gate.
6. If a meaningful discrepancy is detected, mark image as `needs_review` rather than silently publishing.

Block 6A enhancement output remains unapproved and cannot affect media fallback,
hero-photo selection or catalog readiness. Human review and effective enhanced
media preference are separate later decisions. Processed files are
content-addressed under `storage/processed`; a database failure after a safe file
write may leave a recoverable orphan. Such files are not deleted automatically
because identical output bytes may be shared by multiple audit rows.

Block 6B keeps approval and presentation selection separate. Immutable
DerivedImageReview events determine current approval from the latest
`created_at`, then UUID. A single mutable PhotoPresentationPreference may select
one currently approved DerivedImage for a source Photo; no row or a null
selection means use the original. Rejecting the selected image atomically clears
that selection. Catalog readiness still chooses the source hero Photo first,
then resolves its effective original or explicitly selected derived
presentation. A missing preferred derived file falls back to the available
original with a warning, while a derived file never rescues a missing original.

## 8. Extraction policy

Primary path:
- multimodal model
- structured schema
- per-field evidence/confidence where useful

Fallback path:
- OCR or targeted re-crop only when confidence/evidence is insufficient or a critical numeric field is ambiguous

The system must never infer a critical commercial field purely from product familiarity.

## 9. Storage

Initial deployment:
- SQLite database
- local filesystem assets

Each stored image keeps:
- stable ID
- checksum
- path
- type/role
- optional parent asset reference

Checksums support deduplication and caching without requiring a complex content-addressed storage hierarchy in the first implementation.

CatalogSnapshot asset locators are portable paths relative to the canonical
storage root (for example `originals/ab/<sha>.png` or
`processed/cd/<sha>.png`). Snapshot creation rejects external, traversing or
non-content-addressed paths and verifies bytes, checksum, decoded format and
persisted dimensions. Historical rendering relies on these immutable
content-addressed files; external deletion of historical assets breaks
re-renderability and is outside normal application semantics.

SQLite configuration should favor:
- WAL mode
- short transactions
- no DB transaction held open during external model calls

## 10. Jobs and idempotency

Expensive work runs outside the HTTP request lifecycle.

A single local worker is enough initially.

Idempotency keys should eventually include:

```text
input checksum + step version + relevant parameter hash
```

This prevents accidental repeated spending when retrying the same operation.

## 11. UI responsibilities

React remains intentionally thin.

Primary screens:
1. Catalog collections
2. Upload / intake
3. Processing progress
4. Product review
5. Price entry
6. Image comparison / approval
7. Catalog configuration
8. Catalog preview / generation

The backend should expose UI-oriented derived statuses rather than forcing React to understand internal workflow details.

## 12. PDF rendering

Catalog generation is deterministic.

```text
CatalogSnapshot payload
       ↓
Validated render DTO
       ↓
HTML/CSS template
       ↓
PDF renderer
```

Renderer must not read live product/price tables directly.

## 13. Skills and prompts

Two initial Codex Skills:
- `product-extraction`
- `catalog-image`

Do not create speculative skills before they solve a demonstrated need.

Runtime prompts belong under:

```text
backend/app/ai/prompts/
```

Codex Skills belong under:

```text
skills/
```

## 14. Initial implementation order

1. repository scaffold and architecture freeze
2. domain data contracts
3. SQLite + migrations
4. photo intake + checksum/dedup
5. durable jobs + worker
6. vision extraction contract
7. normalization/classification
8. review/pricing API
9. image edit pipeline
10. React review interface
11. catalog snapshot
12. HTML/CSS templates
13. PDF renderer
14. uncertainty recovery improvements
15. lightweight evaluation set and regression checks

This is one final-product architecture built in blocks, not three disposable product versions.

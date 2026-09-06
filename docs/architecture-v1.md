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

### SKU
The sellable variant. Price belongs here.

Typical variant dimensions:
- flavor
- presentation/weight
- serving count
- format

### Photo
One SKU may have N photos.

Optional role:
- front
- side
- back
- nutrition
- other

### ExtractionRun
Stores one AI extraction attempt, including:
- model version
- prompt version
- raw response reference
- usage/cost metadata
- timestamp

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

### CatalogVersion
Immutable snapshot containing exactly what was published:
- selected SKUs
- displayed names
- displayed prices
- referenced image assets
- template/version
- generated PDF reference

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
  required extraction fields accepted
  AND classification accepted
  AND image accepted
  AND approved active price exists
  AND review complete
```

## 7. Image policy

The system intentionally uses AI image editing because high-quality catalog presentation is a core product feature.

Rules:
1. Original photo is immutable and always preserved.
2. Edited image is stored as a separate derived asset.
3. Editing prompts explicitly request preservation of packaging identity, label, logo, proportions and visible text.
4. UI shows original and edited assets side by side before approval.
5. Automated comparison may assist review, but it is not a mandatory pixel-identity gate.
6. If a meaningful discrepancy is detected, mark image as `needs_review` rather than silently publishing.

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
CatalogVersion snapshot
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

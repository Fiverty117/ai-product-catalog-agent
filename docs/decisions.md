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

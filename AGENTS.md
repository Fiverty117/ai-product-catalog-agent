# AGENTS.md

## Project purpose
Build a local-first, single-user AI Product Catalog Agent that converts product photos into reviewed, priced, branded PDF catalogs.

## Working method

1. Build against the frozen Architecture v1.0; do not create disposable prototype architectures.
2. Implement one coherent block at a time.
3. Spend the large majority of effort on implementation, with only focused smoke tests and essential gates per block.
4. Avoid broad refactors, exhaustive test suites, infrastructure expansion, or speculative abstractions unless a concrete problem requires them.
5. Before changing architecture-level decisions, explain the reason and the affected modules.

## AI vs deterministic responsibilities

Use deterministic code for:
- workflow orchestration
- state transitions
- dependency resolution
- unit normalization
- pricing persistence
- approval rules
- catalog snapshot creation
- PDF layout/rendering
- retry limits and idempotency

Use AI for:
- reading and interpreting product images
- extracting structured visible product metadata
- uncertain category suggestion when rules are insufficient
- high-fidelity product image editing
- optional recovery behavior for ambiguous visual inputs

## Critical product rules

- Never invent prices.
- Never overwrite a human-locked field with a model result.
- Preserve original uploaded images.
- Store edited images separately.
- If information is unreadable, absent, pending, or failed, preserve that distinction instead of collapsing everything to null.
- Support multiple photos per SKU.
- Historical catalog versions must be reproducible from immutable snapshots.

## Scope discipline

Do not add unless explicitly justified:
- Kubernetes
- microservices
- Redis
- Celery
- remote PostgreSQL
- multi-user authentication
- billing
- SaaS infrastructure
- multi-agent frameworks

## Verification rule

For each implementation block:
- run only the essential smoke tests for the code touched;
- report what was verified;
- do not spend more effort on verification than implementation unless a real defect appears.

# Product Extraction Skill

## Purpose
Guide Codex when working on product-image extraction logic in this repository.

## Principles
- Extract only information supported by the supplied image set.
- Never infer price.
- Preserve uncertainty explicitly.
- Important fields should support value, source, confidence, evidence, status and locked state where the domain model requires it.
- Human-locked values must never be overwritten by re-extraction.
- Prefer structured, schema-validated outputs.
- OCR is a fallback for uncertain/critical fields, not a mandatory duplicate pass for every image.

## Expected extraction concepts
- brand
- product line/name
- SKU variant
- flavor
- presentation / weight
- servings
- format (powder, capsule, gummy, etc.)
- visible claims when explicitly present
- candidate category

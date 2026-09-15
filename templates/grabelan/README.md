# Grabelan catalog template

This directory contains the versioned `grabelan-catalog-v1` HTML/CSS template.
Its current visual role is a neutral two-column "Classic" catalog layout.

The renderer consumes an immutable CatalogSnapshot, never live commerce tables.

Brand assets should be added only when they are intentionally safe to publish in the repository.
No logo asset is currently present, so v1 uses a plain text store heading.

Layout colors, surfaces, borders, spacing and radius are consolidated as a small
set of CSS custom properties. They are layout defaults, not a persisted branding
profile or theming framework. A future BrandProfile may supply publisher identity
and approved brand values without changing Product card or variant structure.

Product cards keep all included SKU variants together. Images use contained,
uncropped presentation; variant labels wrap independently from non-truncating
prices.

The template groups ordered Products into explicit print-safe rows before CSS
layout. `products_per_row` is defined once in the Jinja template and inherited by
row grids through `--products-per-row`; Classic v1 currently uses two equal
columns. Categories may flow naturally across a page, while complete Product
rows, cards and variant rows carry conservative print-break rules. Future 3- or
4-column templates can reuse this row-fragmentation structure while separately
tuning their visual density.

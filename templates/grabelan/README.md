# Grabelan catalog template

This directory contains the versioned `grabelan-catalog-v1` HTML/CSS template.
Its current visual role is a neutral two-column "Classic" catalog layout.

The renderer consumes an immutable CatalogSnapshot, never live commerce tables.

The directory/key remain named `grabelan-catalog-v1` for historical template
lineage, but the Jinja content no longer hardcodes the publisher. Branded v2
renders consume resolved CatalogBrandProfile values frozen in the Job and run;
verified logo bytes are embedded offline, with display-name text fallback.
Block 8C may rename/register generic Classic/Dense/Compact layout keys without
rewriting historical v1 lineage or duplicating this template now.

Layout colors, surfaces, borders, spacing and radius are consolidated as a small
set of CSS custom properties. They are layout defaults, not a persisted branding
profile or theming framework. Validated CatalogBrandProfile hex colors cross
only a narrow `--brand-primary` / `--brand-accent` CSS-variable boundary, without
changing Product card or variant structure.

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

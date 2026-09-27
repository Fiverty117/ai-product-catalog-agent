# Runtime AI prompts

Prompts in this directory are used by the catalog application at runtime.

They are separate from repository-level Codex Skills under `/skills`.

Each production prompt should eventually carry a stable version identifier so extraction runs can be reproduced and compared.

Product Copy prompts retain their historical versions because queued jobs store
the prompt identifier and resolve its text when a worker executes them. The
latest version is the default for new `product.copy.v1` jobs; older versions
remain available only to preserve their original execution semantics.

`product-copy-v4` makes exact Product identity and form the primary source of
commercial meaning. It retains v3's general-knowledge policy and SKU exclusion,
while queued v1-v3 jobs continue to resolve their original prompt text.

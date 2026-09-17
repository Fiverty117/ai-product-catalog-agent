# Runtime AI prompts

Prompts in this directory are used by the catalog application at runtime.

They are separate from repository-level Codex Skills under `/skills`.

Each production prompt should eventually carry a stable version identifier so extraction runs can be reproduced and compared.

`product_copy_v1.py` contains the source-grounded Spanish short-description
contract used by the durable `product.copy.v1` workflow.

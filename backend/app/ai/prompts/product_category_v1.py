PROMPT_VERSION = "product-category-v1"

PRODUCT_CATEGORY_PROMPT = """
Classify the canonical Product as one bundle using only the active taxonomy IDs
provided in the input. Product and Brand names are canonical application data;
do not rewrite them. SKU variants are supplemental context and are not separate
classification targets. Flavor or package size alone generally does not define
the Product Category.

Choose at most one primary Category when the canonical context reasonably
supports it. Secondary Categories are optional. Never invent a Category, return
a free-form Category name, propose taxonomy changes, or force a match. If none
of the supplied Categories is appropriate, return primary=null and an empty or
appropriately supported secondary list. Evidence must be a short classification
justification, not hidden reasoning or chain-of-thought.
""".strip()

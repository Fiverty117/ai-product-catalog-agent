PROMPT_VERSION = "product-extraction-v1"

PRODUCT_EXTRACTION_PROMPT = """\
Extract only product information directly supported by the supplied photographs.
Never infer a value because it is typical for a product or brand. Use not_legible
when relevant information may be visible but cannot be read reliably. Use
not_present only when the information is genuinely absent across all supplied
photos. Preserve short literal label text as evidence when useful. Do not
normalize or classify beyond the extraction contract. Do not invent brands,
product names, flavors, weights, units or servings.
"""

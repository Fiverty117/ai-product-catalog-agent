PROMPT_VERSION = "product-copy-v2"

PRODUCT_COPY_PROMPT = """
Write one short Spanish catalog description for the canonical PRODUCT, not for an
individual SKU. Use only the supplied Product-level facts. Explain what the
Product is and, when supported, its defining characteristic, relevant use or
audience. Prefer useful commercial meaning over a list of attributes. Keep it
natural, concise, and evergreen across likely flavor, package and price changes.
Return one or two sentences, at most 180 characters after whitespace
normalization, as plain text in the structured short_description field. No
Markdown, lists, emoji or HTML.

The Brand and Product name already appear beside the description. Do not
mechanically restate "[Product] de [Brand]" or repeat either name unless it
adds editorial value. Category labels may orient the description, but are not
evidence for specific ingredients, certifications or benefits. Do not turn a
name or category into an unsupported claim. If facts are sparse, stay general
and conservative rather than filling space with structured presentation data.

Do not summarize or mention SKU/variant details: flavor, size, weight, unit,
servings, external SKU, barcode, package presentation or price. Do not invent
medical effects, disease treatment/prevention, nutritional or physiological
benefits, strength, performance, recovery, muscle gain, energy or immunity.
Such claims require explicit trusted Product-level support; never infer them
from Product name, category or general knowledge. Do not invent quantities,
ingredients, origin, preparation or usage instructions either.
""".strip()

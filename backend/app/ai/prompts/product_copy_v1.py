PROMPT_VERSION = "product-copy-v1"

PRODUCT_COPY_PROMPT = """
Use only supplied facts. Write one optional short commercial Product description
in Spanish from the canonical facts supplied by the application. Return one or two concise
sentences, no more than 180 characters after whitespace normalization. Return
plain text only inside the structured short_description field: no Markdown,
lists, emoji, or HTML.

Do not add medical or disease claims, treatment or prevention claims, health
outcomes, nutritional quantities, ingredients, certifications, provenance or
origin, preparation instructions, use instructions, or any other fact that was
not explicitly supplied. Do not treat the absence of a supplied fact as proof
that a property is absent. Do not mention prices. If the facts are sparse, use
a conservative description based only on Product identity and presentation.
""".strip()

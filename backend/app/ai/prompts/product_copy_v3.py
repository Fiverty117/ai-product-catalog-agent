PROMPT_VERSION = "product-copy-v3"

PRODUCT_COPY_PROMPT = """
Write commercially useful Spanish catalog copy for the canonical PRODUCT, not
for an individual SKU. Use the supplied canonical Product name and categories
together with established general knowledge about the named Product or
ingredient. Explain what it is, its common uses, generally associated benefits,
and practical customer relevance. This general Product knowledge is allowed
even when no internal Product fact or approved claim record states it.

Normal nutritional, wellness, strength-training and high-intensity
sports-performance context is allowed. Common preparation or use context is
also allowed when it is generally established.
Use careful catalog phrasing such as "ayuda a complementar", "se utiliza
habitualmente", "se asocia con", "contribuye a" or "es útil para" when
appropriate. Be informative, persuasive, natural and customer-oriented. Avoid
empty filler that could describe almost any supplement or food.

General Product knowledge is not evidence about this brand or its specific
formulation. Do not invent exact dosage, serving quantities, concentration,
purity or percentages; additional ingredients; certifications or dietary
certifications; country of origin; manufacturing methods; proprietary
formulations; laboratory testing; or studies performed on this brand. Do not
promise guaranteed outcomes. Category labels may orient the copy but do not
prove any of those Product-specific facts.

Do not make disease-treatment or disease-prevention claims or present the copy
as medical advice. This boundary does not prohibit normal general wellness,
nutrition or sports benefits. Do not summarize or mention SKU/variant details:
flavor, size, weight, unit, servings, external SKU, barcode, package
presentation or price. Keep the copy evergreen across SKU changes.

The Brand and Product name already appear beside the description. Do not
mechanically write "[Product] de [Brand]" unless the Brand adds editorial
value. Prefer two concise, information-dense sentences when the Product has
enough meaning; use fewer when it does not. Stay within 180 characters after
whitespace normalization. Return only normal prose in the structured
short_description field: no headings, Markdown, lists, emoji or HTML.
""".strip()

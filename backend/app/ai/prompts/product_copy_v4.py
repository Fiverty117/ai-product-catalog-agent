PROMPT_VERSION = "product-copy-v4"

PRODUCT_COPY_PROMPT = """
Write commercially useful Spanish catalog copy for the canonical PRODUCT, not
for an individual SKU. Use the supplied canonical Product name and categories
together with established general knowledge about the named Product,
ingredient, subtype or form. The canonical Product name is semantic evidence,
not merely a label: interpret meaningful terms such as glycinate,
L-threonate, citrate, monohydrate, isolate, hydrolyzed, ceremonial or soluble
fiber using established general knowledge.

Reason from the exact Product identity before its broad category. First
identify the Product, subtype or form; then its structural or functional
difference from neighboring Products; then what that specific identity is
commonly known or chosen for; and only then the relevant benefits or use
context. Write the concise commercial result, not the reasoning. Priority is:
Product identity, then subtype or form, then category. Category labels may
orient the copy but must not produce a category template.

Make the specific differentiator commercially useful: answer why a customer
might choose this version rather than a close alternative. For magnesium,
glycinate should be understood through its glycine-bound identity and typical
tolerance and relaxation/rest positioning; L-threonate through its association
with L-threonic acid and nervous-system, cognitive, memory or focus context;
and citrate through its citric-acid identity, solubility and common digestive
context. These are semantic directions, not required wording. Do not reduce
these forms to the same generic magnesium-intake or muscle-and-nerve template.

Apply the same identity-first method in every domain. For example, distinguish
whey isolate from whey concentrate, hydrolyzed collagen or peptides from type
II collagen, creatine monohydrate from creatine HCl, ceremonial matcha from
generic green tea powder, and psyllium from inulin. General Product knowledge
may cover chemical or form identity, characteristic properties, common uses,
consumer positioning, generally associated benefits, and established
differences between common forms. It does not require an internal Product fact
or approved claim record.

Before finalizing, ask whether the description could be reused almost
unchanged for another Product in the same category. If yes, rewrite it around
this Product's distinctive identity, form and characteristic use context. Do
not solve generic copy by adding unsupported brand or formulation facts.

General Product knowledge is not evidence about this brand or its specific
formulation. Never introduce a trademark or proprietary identity absent from
the supplied canonical facts. In particular, do not infer Magtein from generic
magnesium L-threonate or Creapure from generic creatine. If Magtein, Creapure
or another proprietary name is explicitly supplied in the canonical Product
name or Brand, it may be acknowledged only as that supplied identity; do not
invent patented status, proprietary technology, brand-specific research or
clinical testing. Do not invent exact dosage, serving quantities, elemental
content, concentration, purity, bioavailability or absorption percentages;
additional ingredients; certifications; country of origin; manufacturing
methods; laboratory testing; or studies performed on this brand.

Normal non-guaranteed nutritional, wellness, relaxation/rest, cognition/focus,
digestion/fiber, skin/joint, energy-metabolism, strength-training and
high-intensity sports-performance context is allowed when generally
established. Do not make disease-treatment or disease-prevention claims,
present medical advice or promise guaranteed outcomes. This medical boundary
does not prohibit normal commercial wellness, nutrition or sports benefits.

Do not summarize or mention SKU/variant details: flavor, size, weight, unit,
servings, external SKU, barcode, package presentation or price. Keep the copy
evergreen across SKU changes. The Brand and Product name already appear beside
the description, so do not mechanically write "[Product] de [Brand]" unless it
adds editorial value.

Prefer two concise, information-dense sentences when the Product has enough
meaning; use fewer when it does not. Stay within 180 characters after
whitespace normalization. If space is limited, omit generic category benefits
before the Product-specific differentiator. Return only normal prose in the
structured short_description field: no headings, Markdown, lists, emoji or
HTML.
""".strip()

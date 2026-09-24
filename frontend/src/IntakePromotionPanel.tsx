import { useRef, useState } from "react";

import {
  ApiError, IntakeItem, IntakePromotionContext, IntakePromotionResult,
  createIntakePromotion, resolveApiUrl,
} from "./api";

function variantLabel(sku: IntakeItem["draft"]["skus"][number], index: number): string {
  return [sku.flavor, sku.size_value && sku.size_unit ? `${sku.size_value} ${sku.size_unit}` : null, sku.external_sku]
    .filter(Boolean).join(" · ") || `Variant ${index + 1}`;
}

export function IntakePromotionPanel({ item, context, onCancel, onCreated }: {
  item: IntakeItem;
  context: IntakePromotionContext;
  onCancel: () => void;
  onCreated: (result: IntakePromotionResult) => void;
}) {
  const [primary, setPrimary] = useState<string>("");
  const [secondary, setSecondary] = useState<string[]>([]);
  const [amounts, setAmounts] = useState<string[]>(item.draft.skus.map(() => ""));
  const [currencies, setCurrencies] = useState<string[]>(item.draft.skus.map(() => "PYG"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);
  const actionKey = useRef<string | null>(null);

  function setAmount(index: number, value: string) {
    actionKey.current = null;
    setAmounts((current) => current.map((amount, row) => row === index ? value : amount));
  }

  function setCurrency(index: number, value: string) {
    actionKey.current = null;
    setCurrencies((current) => current.map((currency, row) => row === index ? value.toUpperCase() : currency));
  }

  async function confirm() {
    if (submitting.current) return;
    const skuPrices = [];
    for (let index = 0; index < amounts.length; index += 1) {
      const amount = amounts[index].trim();
      if (!amount) continue;
      if (!/^\d+(?:\.\d{1,4})?$/.test(amount) || Number(amount) <= 0) {
        setError(`Variant ${index + 1}: enter a positive Price with at most four decimal places.`);
        return;
      }
      if (!/^[A-Z]{3}$/.test(currencies[index])) {
        setError(`Variant ${index + 1}: enter a three-letter currency code.`);
        return;
      }
      skuPrices.push({ intake_sku_index: index, amount, currency: currencies[index] });
    }
    submitting.current = true;
    setBusy(true); setError(null);
    actionKey.current ??= crypto.randomUUID();
    try {
      const result = await createIntakePromotion(item.id, {
        idempotency_key: actionKey.current,
        primary_category_id: primary || null,
        secondary_category_ids: secondary,
        sku_prices: skuPrices,
      });
      actionKey.current = null;
      onCreated(result);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) actionKey.current = null;
      setError(reason instanceof Error ? reason.message : "Product creation failed.");
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  const primaryPhoto = item.photos.find((photo) => photo.is_primary);
  return <section className="intake-panel intake-confirm" aria-label="Review canonical product promotion">
    <h2>Review canonical product</h2>
    <p>Confirm this saved draft before creating a new Brand/Product/SKU graph. The Intake will become read-only.</p>
    {error && <p role="alert" className="intake-error">{error}</p>}
    <dl className="intake-review-facts"><div><dt>Brand</dt><dd>{item.draft.brand_name}</dd></div><div><dt>Product</dt><dd>{item.draft.product_name}</dd></div><div><dt>Source image</dt><dd>{primaryPhoto?.original_filename ?? "None"}</dd></div></dl>
    {primaryPhoto && <img className="intake-review-image" src={resolveApiUrl(primaryPhoto.image_url)} alt="Primary source photo" />}
    <p>All {item.photos.length} source photos become Product-owned; {primaryPhoto?.original_filename ?? "the primary photo"} becomes the front Photo. No photo is assigned to an SKU.</p>
    <div className="intake-fields">
      <label>Canonical primary Category
        <select disabled={busy} value={primary} onChange={(event) => { actionKey.current = null; setPrimary(event.target.value); setSecondary((current) => current.filter((id) => id !== event.target.value)); }}>
          <option value="">No Category — Product will not be catalog-ready</option>
          {context.categories.map((category) => <option key={category.id} value={category.id}>{category.name}</option>)}
        </select>
      </label>
    </div>
    <p>Draft suggestion: {item.draft.primary_category_name || "None"}. Select a canonical Category explicitly; suggestions do not create taxonomy.</p>
    <fieldset className="intake-categories"><legend>Secondary canonical Categories</legend>
      {context.categories.filter((category) => category.id !== primary).map((category) => <label key={category.id}><input disabled={busy} type="checkbox" checked={secondary.includes(category.id)} onChange={(event) => { actionKey.current = null; setSecondary((current) => event.target.checked ? [...current, category.id] : current.filter((id) => id !== category.id)); }} />{category.name}</label>)}
      {context.categories.length === 0 && <p>No active Categories available.</p>}
    </fieldset>
    <h3>Variants and optional initial Prices</h3>
    {item.draft.skus.map((sku, index) => <div className="intake-price-row" key={index}>
      <strong>{variantLabel(sku, index)}</strong>
      <div className="intake-fields"><label>Price for variant {index + 1}<input disabled={busy} aria-label={`Price for variant ${index + 1}`} inputMode="decimal" value={amounts[index]} onChange={(event) => setAmount(index, event.target.value)} placeholder="Leave blank" /></label>
        <label>Currency for variant {index + 1}<input disabled={busy} aria-label={`Currency for variant ${index + 1}`} maxLength={3} value={currencies[index]} onChange={(event) => setCurrency(index, event.target.value)} /></label></div>
      {!amounts[index].trim() && <p>No price — Product may not be catalog-ready.</p>}
    </div>)}
    {!primary && <p className="intake-notice">No primary Category selected — Product will be Not ready.</p>}
    <div className="intake-actions"><button disabled={busy} onClick={() => void confirm()}>{busy ? "Creating product..." : "Create canonical product"}</button><button className="secondary-button" disabled={busy} onClick={onCancel}>Cancel</button></div>
  </section>;
}

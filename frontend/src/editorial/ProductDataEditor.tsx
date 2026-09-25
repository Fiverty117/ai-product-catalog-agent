import { useState } from "react";
import type { ProductDataSummary, SKUDataInput } from "../api";

type EditMode = "identity" | "categories" | "add-sku" | `sku:${string}` | `price:${string}` | null;
type SaveAction =
  | { kind: "identity"; name: string; brandId: string }
  | { kind: "categories"; primaryId: string | null; secondaryIds: string[] }
  | { kind: "sku"; skuId: string | null; fields: SKUDataInput }
  | { kind: "price"; skuId: string; amount: string; currency: string };

const blankSKU = (): SKUDataInput => ({ external_sku: null, flavor: null, size_value: null, size_unit: null, servings: null });
const cleanSKU = (fields: SKUDataInput): SKUDataInput => ({
  external_sku: fields.external_sku?.trim() || null,
  flavor: fields.flavor?.trim() || null,
  size_value: fields.size_value?.trim() || null,
  size_unit: fields.size_unit?.trim() || null,
  servings: fields.servings,
});
const formatPYG = (amount: string) => `Gs. ${amount.split(".")[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".")}`;

export function ProductDataEditor({ data, saving, onSave }: {
  data: ProductDataSummary;
  saving: boolean;
  onSave: (action: SaveAction) => Promise<boolean>;
}) {
  const [mode, setMode] = useState<EditMode>(null);
  const [name, setName] = useState("");
  const [brandId, setBrandId] = useState("");
  const [primaryId, setPrimaryId] = useState("");
  const [secondaryIds, setSecondaryIds] = useState<string[]>([]);
  const [skuDraft, setSKUDraft] = useState<SKUDataInput>(blankSKU);
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("PYG");
  const [validation, setValidation] = useState<string | null>(null);
  const begin = (next: EditMode) => { setMode(next); setValidation(null); };
  const cancel = () => { setMode(null); setValidation(null); };
  const submit = async (action: SaveAction) => {
    if (saving) return;
    if (await onSave(action)) cancel();
  };
  const skuFields = (sku: SKUDataInput) => (
    <div className="product-data-fields">
      {(["flavor", "size_value", "size_unit", "servings", "external_sku"] as const).map((field) => (
        <label key={field}>{field.replace("_", " ")}
          <input
            aria-label={field.replace("_", " ")}
            type={field === "size_value" || field === "servings" ? "number" : "text"}
            min={field === "size_value" || field === "servings" ? "0.000001" : undefined}
            step={field === "servings" ? "1" : field === "size_value" ? "any" : undefined}
            value={sku[field] ?? ""} disabled={saving}
            onChange={(event) => setSKUDraft((current) => ({ ...current, [field]: field === "servings" ? (event.target.value === "" ? null : Number(event.target.value)) : event.target.value }))}
          />
        </label>
      ))}
    </div>
  );
  const saveSKU = (skuId: string | null) => {
    const fields = cleanSKU(skuDraft);
    if ((fields.size_value === null) !== (fields.size_unit === null) || (fields.size_value !== null && Number(fields.size_value) <= 0) || (fields.servings !== null && (!Number.isInteger(fields.servings) || fields.servings <= 0))) {
      setValidation("Enter a positive size with its unit and a positive whole servings count.");
      return;
    }
    void submit({ kind: "sku", skuId, fields });
  };
  const actions = (save: () => void) => <div className="product-data-actions">
    <button type="button" disabled={saving} onClick={cancel}>Cancel</button>
    <button type="button" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save changes"}</button>
  </div>;

  return <section className="editorial-section product-data" aria-label="Product data">
    <p className="section-kicker">Product data</p>
    <h3>Canonical details</h3>
    {validation && <div className="notice notice-error" role="alert">{validation}</div>}

    <div className="product-data-group">
      <h4>Product</h4>
      {mode === "identity" ? <div className="product-data-fields">
        <label>Name<input aria-label="Product name" value={name} maxLength={255} disabled={saving} onChange={(event) => setName(event.target.value)} /></label>
        <label>Brand<select aria-label="Product Brand" value={brandId} disabled={saving} onChange={(event) => setBrandId(event.target.value)}>
          {data.brands.map((brand) => <option key={brand.brand_id} value={brand.brand_id}>{brand.name}</option>)}
        </select></label>
        {actions(() => name.trim() ? void submit({ kind: "identity", name: name.trim(), brandId }) : setValidation("Product name is required."))}
      </div> : <div>
        <p>{data.product.product_name} · {data.product.brand_name}</p>
        <button type="button" onClick={() => { setName(data.product.product_name); setBrandId(data.brand_id); begin("identity"); }}>Edit Product</button>
      </div>}
      {data.identity_history.length > 0 && <details><summary>Identity edit history</summary>
        {data.identity_history.map((edit, index) => <p key={`${edit.created_at}-${index}`}>{edit.old_name} → {edit.new_name} · {new Date(edit.created_at).toLocaleString()}</p>)}
      </details>}
    </div>

    <div className="product-data-group">
      <h4>Categories</h4>
      <p>Need a new canonical Category? <a href="/categories" target="_blank" rel="noopener noreferrer">Manage categories</a>, then reopen this Product to refresh the choices.</p>
      {mode === "categories" ? <div>
        <label>Primary Category<select aria-label="Primary Category" value={primaryId} disabled={saving} onChange={(event) => { setPrimaryId(event.target.value); setSecondaryIds((ids) => ids.filter((id) => id !== event.target.value)); }}>
          <option value="">None</option>{data.categories.map((category) => <option key={category.category_id} value={category.category_id}>{category.name}</option>)}
        </select></label>
        <fieldset><legend>Secondary Categories</legend>{data.categories.filter((category) => category.category_id !== primaryId).map((category) => <label key={category.category_id}>
          <input type="checkbox" checked={secondaryIds.includes(category.category_id)} disabled={saving} onChange={(event) => setSecondaryIds((ids) => event.target.checked ? [...ids, category.category_id] : ids.filter((id) => id !== category.category_id))} />{category.name}
        </label>)}</fieldset>
        {actions(() => void submit({ kind: "categories", primaryId: primaryId || null, secondaryIds }))}
      </div> : <div>
        <p>Primary: {data.categories.find((category) => category.is_primary)?.name ?? "None"}</p>
        <p>Secondary: {data.categories.filter((category) => category.assigned && !category.is_primary).map((category) => category.name).join(", ") || "None"}</p>
        <button type="button" onClick={() => { setPrimaryId(data.categories.find((category) => category.is_primary)?.category_id ?? ""); setSecondaryIds(data.categories.filter((category) => category.assigned && !category.is_primary).map((category) => category.category_id)); begin("categories"); }}>Edit Categories</button>
      </div>}
    </div>

    <div className="product-data-group">
      <h4>Variants and pricing</h4>
      {data.skus.map((sku) => <div className="product-data-sku" key={sku.sku_id}>
        <strong>{[sku.flavor, sku.size_value && sku.size_unit ? `${sku.size_value} ${sku.size_unit}` : null, sku.external_sku].filter(Boolean).join(" · ") || "Standard variant"}</strong>
        {mode === `sku:${sku.sku_id}` ? <div>{skuFields(skuDraft)}{actions(() => saveSKU(sku.sku_id))}</div> : <div>
          <p>Servings: {sku.servings ?? "—"} · External SKU: {sku.external_sku ?? "—"}</p>
          <button type="button" onClick={() => { setSKUDraft({ external_sku: sku.external_sku, flavor: sku.flavor, size_value: sku.size_value, size_unit: sku.size_unit, servings: sku.servings }); begin(`sku:${sku.sku_id}`); }}>Edit variant</button>
        </div>}
        <p>Active PYG price: {sku.active_price ? formatPYG(sku.active_price.amount) : "None"}</p>
        {mode === `price:${sku.sku_id}` ? <div className="product-data-fields">
          <label>Amount<input aria-label="Price amount" type="number" min="0" step="any" value={amount} disabled={saving} onChange={(event) => setAmount(event.target.value)} /></label>
          <label>Currency<input aria-label="Price currency" value={currency} maxLength={3} disabled={saving} onChange={(event) => setCurrency(event.target.value.toUpperCase())} /></label>
          {actions(() => amount !== "" && Number(amount) >= 0 && /^[A-Z]{3}$/.test(currency) ? void submit({ kind: "price", skuId: sku.sku_id, amount, currency }) : setValidation("Enter a nonnegative amount and a three-letter currency."))}
        </div> : <button type="button" onClick={() => { setAmount(sku.active_price?.amount ?? ""); setCurrency("PYG"); begin(`price:${sku.sku_id}`); }}>Change price</button>}
        {sku.price_history.length > 0 && <details><summary>Price history</summary>{sku.price_history.map((price) => <p key={price.price_id}>{price.currency} {price.amount} · {price.approved ? "Approved" : "Pending"} · {new Date(price.valid_from).toLocaleString()}</p>)}</details>}
      </div>)}
      {mode === "add-sku" ? <div>{skuFields(skuDraft)}{actions(() => saveSKU(null))}</div> : <button type="button" onClick={() => { setSKUDraft(blankSKU()); begin("add-sku"); }}>Add variant</button>}
    </div>
  </section>;
}

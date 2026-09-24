import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  ApiError, IntakeDraft, IntakeItem, IntakeSKU, createIntakeItem, fetchIntakeItem,
  fetchIntakeItems, fetchIntakePromotion, IntakePromotionContext, IntakePromotionResult,
  resolveApiUrl, runIntakeExtraction, saveIntakeDraft,
  setIntakePrimaryPhoto,
} from "./api";
import { IntakePromotionPanel } from "./IntakePromotionPanel";

const labels: Record<IntakeItem["status"], string> = {
  draft: "Draft", queued: "Queued", running: "Extracting",
  review_required: "Review required", failed: "Failed",
  promoted: "Product created",
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Please try again.";
}

function emptySKU(): IntakeSKU {
  return { flavor: null, size_value: null, size_unit: null, servings: null, external_sku: null };
}

function validateDraft(draft: IntakeDraft): string | null {
  for (const [index, sku] of draft.skus.entries()) {
    const hasValue = sku.size_value !== null && sku.size_value.trim() !== "";
    const hasUnit = sku.size_unit !== null && sku.size_unit.trim() !== "";
    if (hasValue !== hasUnit) return `Variant ${index + 1}: size and unit must be provided together.`;
    if (hasValue && (!Number.isFinite(Number(sku.size_value)) || Number(sku.size_value) <= 0))
      return `Variant ${index + 1}: size must be positive.`;
    if (sku.servings !== null && (!Number.isInteger(sku.servings) || sku.servings <= 0))
      return `Variant ${index + 1}: servings must be a positive whole number.`;
  }
  return null;
}

export function ProductIntakePage() {
  const { intakeId } = useParams();
  const navigate = useNavigate();
  const [items, setItems] = useState<IntakeItem[]>([]);
  const [item, setItem] = useState<IntakeItem | null>(null);
  const [draft, setDraft] = useState<IntakeDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [promotionContext, setPromotionContext] = useState<IntakePromotionContext | null>(null);
  const [promotionResult, setPromotionResult] = useState<IntakePromotionResult | null>(null);
  const [showPromotionReview, setShowPromotionReview] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const actionKey = useRef<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (intakeId) {
      const [fresh, context] = await Promise.all([fetchIntakeItem(intakeId, signal), fetchIntakePromotion(intakeId, signal)]);
      setItem(fresh);
      setPromotionContext(context);
      setPromotionResult(context.result);
      if (!dirty) setDraft(fresh.draft);
    } else {
      setItems((await fetchIntakeItems(signal)).items);
    }
  }, [intakeId, dirty]);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void load(controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setError(message(reason));
    });
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!intakeId || !item || !["queued", "running"].includes(item.status)) return;
    const timer = window.setInterval(() => {
      void fetchIntakeItem(intakeId).then((fresh) => {
        setItem(fresh);
        if (!dirty) setDraft(fresh.draft);
      }).catch((reason) => setError(message(reason)));
    }, 2000);
    return () => window.clearInterval(timer);
  }, [intakeId, item?.status, dirty]);

  async function upload() {
    if (!files.length || busy) return;
    setBusy(true); setError(null);
    try {
      const created = await createIntakeItem(files);
      setFiles([]); setShowCreate(false);
      navigate(`/product-intake/${created.id}`);
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }

  async function extract() {
    if (!item || busy) return;
    setBusy(true); setError(null);
    actionKey.current ??= crypto.randomUUID();
    try {
      const fresh = await runIntakeExtraction(item.id, actionKey.current);
      actionKey.current = null;
      setItem(fresh);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) actionKey.current = null;
      setError(message(reason));
    } finally { setBusy(false); }
  }

  async function save() {
    if (!item || !draft || busy) return;
    const invalid = validateDraft(draft);
    if (invalid) { setError(invalid); return; }
    setBusy(true); setError(null);
    try {
      const normalized = {
        ...draft,
        skus: draft.skus.map((sku) => ({
          ...sku,
          size_value: sku.size_value?.trim() || null,
          size_unit: sku.size_unit?.trim() || null,
        })),
      };
      const fresh = await saveIntakeDraft(item.id, normalized);
      setItem(fresh); setDraft(fresh.draft); setDirty(false);
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }

  async function selectPrimary(photoId: string) {
    if (!item || busy) return;
    setBusy(true); setError(null);
    try { setItem(await setIntakePrimaryPhoto(item.id, photoId)); }
    catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }

  function edit(field: keyof IntakeDraft, value: IntakeDraft[keyof IntakeDraft]) {
    if (draft) { setDraft({ ...draft, [field]: value }); setDirty(true); }
  }

  function editSKU(index: number, field: keyof IntakeSKU, value: IntakeSKU[keyof IntakeSKU]) {
    if (!draft) return;
    const skus = draft.skus.map((sku, row) => row === index ? { ...sku, [field]: value } : sku);
    edit("skus", skus);
  }

  const promoted = item?.status === "promoted";
  const canPromote = Boolean(item && draft && !promoted && !dirty &&
    item.status !== "queued" && item.status !== "running" && item.photos.length > 0 &&
    draft.brand_name?.trim() && draft.product_name?.trim() && draft.skus.length > 0 &&
    draft.skus.every((sku) => Boolean(sku.flavor || sku.size_value || sku.servings || sku.external_sku)) &&
    !validateDraft(draft));
  const created = promotionResult ?? promotionContext?.result;

  if (!intakeId) return (
    <main className="intake-shell">
      <header className="intake-header"><div><p className="eyebrow">Workspace</p><h1>Product Intake</h1><p>Collect photos and review product information before creating a canonical product.</p></div><button onClick={() => setShowCreate(true)}>+ New intake</button></header>
      {error && <p role="alert" className="intake-error">{error}</p>}
      {showCreate && <section className="intake-panel" aria-label="Add product">
        <h2>Add product</h2><p>Choose one or more original product photos. Extraction runs only when you request it.</p>
        <div className="intake-drop" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); setFiles(Array.from(event.dataTransfer.files)); }}>
          <label>Source photos<input aria-label="Source photos" type="file" accept="image/jpeg,image/png,image/webp" multiple onChange={(event) => setFiles(Array.from(event.target.files ?? []))} /></label>
          <small>Or drop JPEG, PNG or WebP images here. Up to 12 photos, 20 MB each.</small>
        </div>
        {files.length > 0 && <ul>{files.map((file, index) => <li key={`${file.name}-${index}`}>{file.name}</li>)}</ul>}
        <div className="intake-actions"><button onClick={() => void upload()} disabled={busy || !files.length}>{busy ? "Uploading…" : "Upload"}</button><button className="secondary-button" onClick={() => { setShowCreate(false); setFiles([]); }} disabled={busy}>Cancel</button></div>
      </section>}
      {items.length === 0 && !showCreate ? <section className="intake-empty"><h2>No products in intake yet</h2><p>Add product photos to start a reviewable draft.</p><button onClick={() => setShowCreate(true)}>Add product</button></section> :
        <div className="intake-grid">{items.map((entry) => <button className="intake-card" key={entry.id} onClick={() => navigate(`/product-intake/${entry.id}`)}>
          {entry.photos[0] ? <img src={resolveApiUrl((entry.photos.find((photo) => photo.is_primary) ?? entry.photos[0]).image_url)} alt="Source product" /> : <span className="intake-placeholder">No photo</span>}
          <span className="intake-card-body"><small>{entry.draft.brand_name || "Brand pending"}</small><strong>{entry.draft.product_name || "Untitled product"}</strong><span>{labels[entry.status]} · {entry.photos.length} photos</span><time>{new Date(entry.updated_at).toLocaleString()}</time></span>
        </button>)}</div>}
    </main>
  );

  return <main className="intake-shell">
    <button className="text-button" onClick={() => navigate("/product-intake")}>← All intake items</button>
    {error && <p role="alert" className="intake-error">{error}</p>}
    {!item || !draft ? <p>Loading intake…</p> : <>
      <header className="intake-header"><div><p className="eyebrow">Product Intake</p><h1>{draft.product_name || "Untitled product"}</h1><p>{draft.brand_name || "Brand pending"} · <span className={`intake-status status-${item.status}`}>{labels[item.status]}</span></p></div></header>
      <section className="intake-panel"><h2>Source photos</h2><p>All source photos are supplied to extraction. The primary photo is used as the workspace thumbnail and becomes the canonical front Photo on promotion.</p>
        <div className="intake-photo-grid">{item.photos.map((photo) => <div className="intake-photo" key={photo.id}><img src={resolveApiUrl(photo.image_url)} alt={photo.original_filename} /><span>{photo.original_filename}</span>{photo.is_primary ? <strong>Primary photo</strong> : !promoted && <button className="secondary-button" disabled={busy} onClick={() => void selectPrimary(photo.id)}>Set as primary</button>}</div>)}</div>
      </section>
      <section className="intake-panel"><h2>Extraction</h2><p>Status: <strong>{labels[item.status]}</strong>{item.extraction.attempts ? ` · Attempt ${item.extraction.attempts}/${item.extraction.max_attempts}` : ""}</p>
        {item.extraction.error && <p role="alert" className="intake-error">{item.extraction.error}</p>}
        {item.extraction.newer_result_available && <p className="intake-notice">A newer extraction is available. Your saved edits were preserved.</p>}
        {item.extraction.run_status === "succeeded" && <p>Latest machine observation is saved separately from this editable draft.</p>}
        {item.extraction.observation && <dl className="intake-observation">{Object.entries(item.extraction.observation).map(([field, observed]) => <div key={field}><dt>{field.replaceAll("_", " ")}</dt><dd>{observed.state === "extracted" ? String(observed.value) : observed.state.replaceAll("_", " ")}</dd></div>)}</dl>}
        {!promoted && <button onClick={() => void extract()} disabled={busy || !item.photos.length || item.status === "queued" || item.status === "running"}>{busy ? "Please wait…" : "Run extraction"}</button>}
      </section>
      <section className="intake-panel"><h2>Draft</h2><p>{promoted ? "Historical Intake Draft — read-only. It no longer changes the canonical Product." : "This working draft does not change canonical products, SKUs, categories or prices."}</p>
        <fieldset className="intake-edit-fieldset" disabled={promoted}>
        <div className="intake-fields">
          <label>Brand<input value={draft.brand_name ?? ""} onChange={(event) => edit("brand_name", event.target.value)} /></label>
          <label>Product name<input value={draft.product_name ?? ""} onChange={(event) => edit("product_name", event.target.value)} /></label>
          <label>Primary category candidate<input value={draft.primary_category_name ?? ""} onChange={(event) => edit("primary_category_name", event.target.value)} /></label>
          <label>Secondary category candidates <small>Comma-separated</small><input value={draft.secondary_category_names.join(", ")} onChange={(event) => edit("secondary_category_names", event.target.value.split(",").map((name) => name.trim()).filter(Boolean))} /></label>
        </div>
        <h3>Variant candidates</h3>
        {draft.skus.map((sku, index) => <div className="intake-sku" key={index}>
          <div className="intake-fields"><label>Flavor<input value={sku.flavor ?? ""} onChange={(event) => editSKU(index, "flavor", event.target.value)} /></label>
            <label>Size<input inputMode="decimal" value={sku.size_value ?? ""} onChange={(event) => editSKU(index, "size_value", event.target.value)} /></label>
            <label>Unit<input value={sku.size_unit ?? ""} onChange={(event) => editSKU(index, "size_unit", event.target.value)} /></label>
            <label>Servings<input type="number" min="1" step="1" value={sku.servings ?? ""} onChange={(event) => editSKU(index, "servings", event.target.value === "" ? null : Number(event.target.value))} /></label>
            <label>External SKU<input value={sku.external_sku ?? ""} onChange={(event) => editSKU(index, "external_sku", event.target.value)} /></label></div>
          <button className="secondary-button" onClick={() => edit("skus", draft.skus.filter((_, row) => row !== index))}>Remove variant</button>
        </div>)}
        <button className="secondary-button" onClick={() => edit("skus", [...draft.skus, emptySKU()])}>Add draft variant</button>
        <label className="intake-notes">Notes<textarea value={draft.notes ?? ""} onChange={(event) => edit("notes", event.target.value)} /></label>
        {!promoted && <div className="intake-actions"><button onClick={() => void save()} disabled={busy || !dirty}>Save draft</button><button className="secondary-button" onClick={() => { setDraft(item.draft); setDirty(false); setError(null); }} disabled={!dirty || busy}>Cancel</button></div>}
        </fieldset>
      </section>
      <section className="intake-panel intake-next"><h2>{promoted ? "Product created" : "Next step"}</h2>
        {created ? <><p><strong>{created.brand_name} · {created.product.product_name}</strong> · {created.sku_ids.length} variants</p><p>{created.product.readiness.ready ? "Ready for catalog" : "Not ready for catalog"} ({created.readiness_currency})</p>
          {!created.product.readiness.ready && <ul>{created.product.readiness.blockers.map((blocker, index) => <li key={`${blocker.code}-${index}`}>{blocker.message}</li>)}</ul>}
          <div className="intake-actions"><button onClick={() => navigate(`/catalog-builder?product=${created.product_id}`)}>Open product</button><button className="secondary-button" onClick={() => navigate("/catalog-builder")}>Open Catalog Builder</button></div>
        </> : <><button disabled={!canPromote || busy || !promotionContext} onClick={() => setShowPromotionReview(true)}>Create product</button>
          <p>{dirty ? "Save the Draft before creating a Product." : !canPromote ? "Complete Brand, Product name and at least one variant to continue." : "Review canonical Categories and optional Prices before confirming."}</p></>}
      </section>
      {showPromotionReview && !promoted && promotionContext && <IntakePromotionPanel item={item} context={promotionContext} onCancel={() => setShowPromotionReview(false)} onCreated={(result) => {
        setPromotionResult(result); setShowPromotionReview(false);
        void fetchIntakeItem(item.id).then((fresh) => { setItem(fresh); setDraft(fresh.draft); setDirty(false); }).catch((reason) => setError(message(reason)));
      }} />}
    </>}
  </main>;
}

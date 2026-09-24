import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  ProductCopyEditorialSummary,
  ProductSummary,
  ProductDataSummary,
  ProductImageEditorialSummary,
  fetchProductData,
  fetchProductImageEditorial,
  generateProductImage,
  retryProductImage,
  reviewProductDerivedImage,
  selectProductImagePresentation,
  saveProductIdentity,
  saveProductCategories,
  saveProductSKU,
  addProductSKU,
  changeProductPrice,
  fetchProductCopyEditorial,
  generateProductCopy,
  resolveApiUrl,
  retryProductCopyGeneration,
  reviewProductCopy,
  saveManualProductCopyRevision,
} from "../api";
import { CurrentProductCopy } from "./CurrentProductCopy";
import { ProductCopyProposal } from "./ProductCopyProposal";
import { ProductDataEditor } from "./ProductDataEditor";
import { ProductImageEditor, ProductImageAction } from "./ProductImageEditor";

function formatPrice(amount: string, currency: string): string {
  if (currency !== "PYG") return `${currency} ${amount}`;
  const integer = amount.split(".")[0];
  return `Gs. ${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".")}`;
}

function EditorialImage({ product }: { product: ProductSummary }) {
  const [failed, setFailed] = useState(false);
  const hero = product.hero;
  const presentationKey = hero ? `${hero.source_photo_id}:${hero.effective_derived_image_id ?? "original"}` : "none";
  useEffect(() => setFailed(false), [presentationKey]);
  if (!hero || failed) {
    return <div className="editorial-image-placeholder">No image</div>;
  }
  return (
    <img
      key={presentationKey}
      src={resolveApiUrl(hero.image_url)}
      alt={`${product.brand_name} ${product.product_name}`}
      onError={() => setFailed(true)}
    />
  );
}

export function ProductEditorialDrawer({
  productId,
  onClose,
  onProductUpdated,
}: {
  productId: string;
  onClose: () => void;
  onProductUpdated: (product: ProductSummary) => void;
}) {
  const [summary, setSummary] = useState<ProductCopyEditorialSummary | null>(null);
  const [productData, setProductData] = useState<ProductDataSummary | null>(null);
  const [imageSummary, setImageSummary] = useState<ProductImageEditorialSummary | null>(null);
  const [imageBusyAction, setImageBusyAction] = useState<string | null>(null);
  const imageRequestKey = useRef<string | null>(null);
  const imageRequestPending = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const applySummary = useCallback(
    (next: ProductCopyEditorialSummary) => {
      setSummary(next);
      onProductUpdated(next.product);
    },
    [onProductUpdated],
  );

  const refresh = useCallback(async () => {
    const next = await fetchProductCopyEditorial(productId);
    applySummary(next);
    return next;
  }, [applySummary, productId]);

  useEffect(() => {
    const controller = new AbortController();
    imageRequestKey.current = null;
    setSummary(null);
    setProductData(null);
    setImageSummary(null);
    setError(null);
    fetchProductCopyEditorial(productId, controller.signal)
      .then(applySummary)
      .catch((caught: unknown) => {
        if ((caught as Error).name !== "AbortError") {
          setError("The Product editorial workspace could not be loaded.");
        }
      });
    fetchProductData(productId, controller.signal)
      .then(setProductData)
      .catch((caught: unknown) => { if ((caught as Error).name !== "AbortError") setError("Product data could not be loaded."); });
    fetchProductImageEditorial(productId, controller.signal)
      .then(setImageSummary)
      .catch((caught: unknown) => { if ((caught as Error).name !== "AbortError") setError("Product image workspace could not be loaded."); });
    return () => controller.abort();
  }, [applySummary, productId]);

  useEffect(() => {
    if (!summary?.has_active_generation) return;
    const interval = window.setInterval(() => {
      void refresh().catch(() => {
        setError("Generation status could not be refreshed.");
      });
    }, 2000);
    return () => window.clearInterval(interval);
  }, [refresh, summary?.has_active_generation]);

  const imageActive = imageSummary?.generations?.some((job) => job.status === "queued" || job.status === "running") ?? false;
  useEffect(() => {
    if (!imageActive) return;
    const controller = new AbortController();
    const interval = window.setInterval(() => {
      void fetchProductImageEditorial(productId, controller.signal)
        .then((next) => { setImageSummary(next); onProductUpdated(next.product); })
        .catch((caught: unknown) => { if ((caught as Error).name !== "AbortError") setError("Image enhancement status could not be refreshed."); });
    }, 2000);
    return () => { window.clearInterval(interval); controller.abort(); };
  }, [imageActive, onProductUpdated, productId]);

  const handleGenerate = async () => {
    setBusyAction("generate");
    setError(null);
    try {
      const response = await generateProductCopy(productId);
      applySummary(response.editorial);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Generation could not be requested.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleRetry = async (jobId: string) => {
    setBusyAction(`retry-${jobId}`);
    setError(null);
    try {
      const response = await retryProductCopyGeneration(productId, jobId);
      applySummary(response.editorial);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Generation could not be retried.");
    } finally {
      setBusyAction(null);
    }
  };

  const handleReview = async (
    runId: string,
    decision: "approved" | "corrected" | "rejected",
    correctedText?: string,
  ) => {
    setBusyAction(runId);
    setError(null);
    try {
      const response = await reviewProductCopy(
        productId,
        runId,
        decision,
        correctedText,
      );
      applySummary(response.editorial);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The proposal could not be reviewed.");
      if (caught instanceof ApiError && caught.status === 409) {
        await refresh().catch(() => undefined);
      }
    } finally {
      setBusyAction(null);
    }
  };

  const handleManualSave = async (text: string) => {
    setBusyAction("manual-save");
    setError(null);
    try {
      const response = await saveManualProductCopyRevision(productId, text);
      applySummary(response.editorial);
      return true;
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The manual edit could not be saved.");
      if (caught instanceof ApiError && caught.status === 409) await refresh().catch(() => undefined);
      return false;
    } finally {
      setBusyAction(null);
    }
  };

  const handleDataSave: React.ComponentProps<typeof ProductDataEditor>["onSave"] = async (action) => {
    setBusyAction("product-data");
    setError(null);
    try {
      let next: ProductDataSummary;
      switch (action.kind) {
        case "identity": next = await saveProductIdentity(productId, action.name, action.brandId); break;
        case "categories": next = await saveProductCategories(productId, action.primaryId, action.secondaryIds); break;
        case "sku": next = action.skuId ? await saveProductSKU(productId, action.skuId, action.fields) : await addProductSKU(productId, action.fields); break;
        case "price": next = await changeProductPrice(productId, action.skuId, action.amount, action.currency); break;
      }
      setProductData(next);
      onProductUpdated(next.product);
      await refresh();
      return true;
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Product data could not be saved or refreshed.");
      return false;
    } finally {
      setBusyAction(null);
    }
  };

  const handleImageAction = async (action: ProductImageAction) => {
    if (!imageSummary?.source_photo_id) return;
    if (imageRequestPending.current) return;
    imageRequestPending.current = true;
    const actionKey = action.kind === "generate" ? "generate-image" : action.kind === "retry" ? `retry-image-${action.jobId}` : action.kind === "review" ? `review-${action.derivedImageId}-${action.decision}` : action.derivedImageId ? `select-${action.derivedImageId}` : "select-original";
    setImageBusyAction(actionKey);
    setError(null);
    try {
      if (action.kind === "generate" || action.kind === "retry") {
        if (action.kind === "generate") imageRequestKey.current ??= crypto.randomUUID();
        const response = action.kind === "generate"
          ? await generateProductImage(productId, imageSummary.source_photo_id, imageRequestKey.current!)
          : await retryProductImage(productId, action.jobId);
        setImageSummary(response.editorial);
        if (action.kind === "generate") imageRequestKey.current = null;
        return;
      }
      const next = action.kind === "review"
        ? await reviewProductDerivedImage(productId, action.derivedImageId, action.decision)
        : await selectProductImagePresentation(productId, imageSummary.source_photo_id, action.derivedImageId);
      setImageSummary(next);
      setSummary((current) => current ? { ...current, product: next.product } : current);
      setProductData((current) => current ? { ...current, product: next.product } : current);
      onProductUpdated(next.product);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The Product image action could not be saved.");
    } finally {
      setImageBusyAction(null);
      imageRequestPending.current = false;
    }
  };

  return (
    <div className="drawer-backdrop">
      <aside
        className="editorial-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="editorial-title"
      >
        <div className="drawer-toolbar">
          <div>
            <p className="section-kicker">Product editorial</p>
            <h2 id="editorial-title">Product data and copy</h2>
          </div>
          <button type="button" className="drawer-close" onClick={onClose} aria-label="Close editorial workspace">
            Close
          </button>
        </div>

        {error && <div className="notice notice-error" role="alert">{error}</div>}
        {!summary && !error && <div className="state-panel" role="status">Loading editorial workspace…</div>}

        {summary && (
          <div className="drawer-content">
            <section className="editorial-product-context" aria-labelledby="editorial-product-name">
              <div className="editorial-product-image"><EditorialImage product={summary.product} /></div>
              <div>
                <p className="product-brand">{summary.product.brand_name}</p>
                <h3 id="editorial-product-name">{summary.product.product_name}</h3>
                <p>{summary.product.primary_category_name ?? "No primary category"}</p>
                <div className="status-row">
                  <span className={`readiness-badge ${summary.product.readiness.ready ? "ready" : "not-ready"}`}>
                    {summary.product.readiness.ready ? "✓ Ready for catalog" : "Not ready"}
                  </span>
                  <span className={`copy-badge copy-${summary.effective_copy.state}`}>
                    Copy: {summary.effective_copy.state.charAt(0).toUpperCase() + summary.effective_copy.state.slice(1)}
                  </span>
                </div>
                <div className="editorial-variants">
                  {summary.product.publishable_skus.map((sku) => (
                    <span key={sku.sku_id}>
                      {sku.variant_label} · {formatPrice(sku.active_price_amount, sku.currency)}
                    </span>
                  ))}
                </div>
              </div>
            </section>

            {imageSummary && <ProductImageEditor summary={imageSummary} busyAction={imageBusyAction} onAction={handleImageAction} />}

            {productData && <ProductDataEditor data={productData} saving={busyAction === "product-data"} onSave={handleDataSave} />}

            <CurrentProductCopy effectiveCopy={summary.effective_copy} saving={busyAction === "manual-save"} onSave={handleManualSave} />

            {summary.manual_revisions.length > 0 && (
              <section className="editorial-section" aria-label="Human edit history">
                <p className="section-kicker">Human edits</p>
                <h3>Manual revision history</h3>
                {summary.manual_revisions.map((revision) => (
                  <div className="generation-activity" key={revision.revision_id}>
                    <strong>Human edit · {revision.source_state}</strong>
                    <span>{revision.short_description}</span>
                    <time dateTime={revision.created_at}>{new Date(revision.created_at).toLocaleString()}</time>
                  </div>
                ))}
              </section>
            )}

            <section className="editorial-section" aria-labelledby="proposal-history-title">
              <div className="editorial-section-heading">
                <div>
                  <p className="section-kicker">AI proposals</p>
                  <h3 id="proposal-history-title">Proposal history</h3>
                </div>
                <button
                  type="button"
                  className="primary-action generate-action"
                  disabled={summary.has_active_generation || busyAction === "generate"}
                  onClick={() => void handleGenerate()}
                >
                  {summary.has_active_generation || busyAction === "generate"
                    ? "Generating…"
                    : "Generate proposal"}
                </button>
              </div>
              <p className="generation-explainer">
                Generated text remains a proposal until you approve or correct it.
              </p>

              {summary.generations
                .filter((generation) => generation.status === "queued" || generation.status === "running")
                .map((generation) => (
                  <div className="generation-activity" role="status" key={generation.job_id}>
                    <strong>{generation.status === "queued" ? "Pending" : "Running"}</strong>
                    <span>Product Copy generation is in progress.</span>
                  </div>
                ))}
              {summary.generations
                .filter((generation) => generation.status === "failed")
                .map((generation) => (
                  <div className="generation-activity generation-failed" key={generation.job_id}>
                    <strong>Generation failed</strong>
                    <span>{generation.error ?? "The worker could not create a proposal."}</span>
                    {generation.can_retry && (
                      <button
                        type="button"
                        disabled={busyAction === `retry-${generation.job_id}`}
                        onClick={() => void handleRetry(generation.job_id)}
                      >
                        Retry generation
                      </button>
                    )}
                  </div>
                ))}

              <div className="proposal-list">
                {summary.runs.length === 0 ? (
                  <div className="proposal-empty">No Product Copy proposals yet.</div>
                ) : (
                  summary.runs.map((run) => (
                    <ProductCopyProposal
                      key={run.run_id}
                      run={run}
                      busy={busyAction === run.run_id}
                      onReview={handleReview}
                    />
                  ))
                )}
              </div>
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}

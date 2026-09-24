import type { ProductImageEditorialSummary } from "../api";
import { resolveApiUrl } from "../api";

export type ProductImageAction =
  | { kind: "select"; derivedImageId: string | null }
  | { kind: "review"; derivedImageId: string; decision: "approved" | "rejected" }
  | { kind: "generate" }
  | { kind: "retry"; jobId: string };

function statusLabel(state: "unreviewed" | "approved" | "rejected") {
  return state === "unreviewed" ? "Pending review" : state === "approved" ? "Approved" : "Rejected";
}

export function ProductImageEditor({ summary, busyAction, onAction }: {
  summary: ProductImageEditorialSummary;
  busyAction: string | null;
  onAction: (action: ProductImageAction) => Promise<void>;
}) {
  const saving = busyAction !== null;
  const activeGeneration = summary.generations?.some((job) => job.status === "queued" || job.status === "running") ?? false;
  if (!summary.source_photo_id || !summary.effective || !summary.original_preview_url) {
    return <section className="editorial-section product-image-editor" aria-label="Product image">
      <p className="section-kicker">Product image</p><h3>Presentation</h3>
      <div className="editorial-copy-message"><strong>No available front image.</strong></div>
    </section>;
  }
  return <section className="editorial-section product-image-editor" aria-label="Product image">
    <p className="section-kicker">Product image</p>
    <h3>Presentation</h3>
    <div className="current-image-presentation">
      <img src={resolveApiUrl(summary.effective.preview_url)} alt="Current Product presentation" />
      <div><strong>Current presentation</strong><span>Using: {summary.effective.presentation === "derived" ? "Enhanced image" : "Original image"}</span></div>
    </div>
    {summary.effective.warnings.length > 0 && <div className="notice" role="status">The preferred enhanced image is unavailable or ineligible. The original is being used.</div>}
    <div className="image-enhancement-section">
      <p className="section-kicker">Image enhancement</p>
      <div className="current-image-presentation">
        <img src={resolveApiUrl(summary.original_preview_url)} alt="Selected source Photo" />
        <div><strong>Current source</strong><span>{summary.source_owner === "sku" ? "SKU front Photo" : "Product front Photo"}</span></div>
      </div>
      <button type="button" className="primary-action" disabled={saving || !summary.can_generate}
        onClick={() => void onAction({ kind: "generate" })}>
        {busyAction === "generate-image" || activeGeneration ? "Enhancing image…" : "Generate enhanced image"}
      </button>
      {!summary.can_generate && !activeGeneration &&
        <p className="generation-explainer">The source Photo is not currently eligible for enhancement.</p>}
      {summary.generations?.map((job) => <div className="generation-activity" key={job.job_id} role={job.status === "queued" || job.status === "running" ? "status" : undefined}>
        <strong>{job.status === "queued" ? "Queued" : job.status === "running" ? "Enhancing image…" : job.status === "succeeded" ? "Image ready" : "Enhancement failed"}</strong>
        {job.status === "failed" && job.can_retry && <button type="button" disabled={saving || activeGeneration} onClick={() => void onAction({ kind: "retry", jobId: job.job_id })}>Retry enhancement</button>}
      </div>)}
    </div>
    <h4>Available images</h4>
    <div className="image-option-grid">
      <article className={`image-option ${summary.effective.presentation === "original" ? "selected" : ""}`}>
        <img src={resolveApiUrl(summary.original_preview_url)} alt="Original source" />
        <strong>Original</strong><span>Original source image</span>
        {summary.effective.presentation === "original" ? <span className="image-status">Selected</span> :
          <button type="button" disabled={saving} onClick={() => void onAction({ kind: "select", derivedImageId: null })}>{busyAction === "select-original" ? "Saving…" : "Use original"}</button>}
      </article>
      {summary.derived_images.map((image) => <article className={`image-option ${image.selected ? "selected" : ""}`} key={image.derived_image_id}>
        {image.preview_url ? <img src={resolveApiUrl(image.preview_url)} alt="Enhanced option" /> : <div className="editorial-image-placeholder">Image unavailable</div>}
        <strong>Enhanced image</strong><span>{statusLabel(image.review_state)}</span>
        {image.selected && <span className="image-status">Selected</span>}
        {image.review_state === "unreviewed" && <div className="product-data-actions">
          <button type="button" disabled={saving || !image.asset_available} onClick={() => void onAction({ kind: "review", derivedImageId: image.derived_image_id, decision: "rejected" })}>{busyAction === `review-${image.derived_image_id}-rejected` ? "Saving…" : "Reject"}</button>
          <button type="button" disabled={saving || !image.asset_available} onClick={() => void onAction({ kind: "review", derivedImageId: image.derived_image_id, decision: "approved" })}>{busyAction === `review-${image.derived_image_id}-approved` ? "Saving…" : "Approve"}</button>
        </div>}
        {image.review_state === "approved" && !image.selected && <button type="button" disabled={saving || !image.selectable} onClick={() => void onAction({ kind: "select", derivedImageId: image.derived_image_id })}>{busyAction === `select-${image.derived_image_id}` ? "Saving…" : "Use enhanced image"}</button>}
        {image.review_state === "rejected" && <button type="button" disabled>Rejected</button>}
      </article>)}
    </div>
  </section>;
}

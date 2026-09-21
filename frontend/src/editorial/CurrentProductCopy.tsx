import { useState } from "react";
import type { ProductCopyEditorialSummary } from "../api";

export function CurrentProductCopy({
  effectiveCopy,
  saving,
  onSave,
}: {
  effectiveCopy: ProductCopyEditorialSummary["effective_copy"];
  saving: boolean;
  onSave: (text: string) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [validation, setValidation] = useState<string | null>(null);
  const save = async () => {
    if (saving || draft === null) return;
    const normalized = draft.trim().replace(/\s+/g, " ");
    if (!normalized || normalized.length > 180) {
      setValidation("Description must contain 1 to 180 characters.");
      return;
    }
    if (await onSave(draft)) setDraft(null);
  };
  return (
    <section className="editorial-section" aria-labelledby="current-copy-title">
      <div className="editorial-section-heading">
        <div>
          <p className="section-kicker">Current effective copy</p>
          <h3 id="current-copy-title">Catalog description</h3>
        </div>
        <span className={`copy-badge copy-${effectiveCopy.state}`}>
          {effectiveCopy.state === "current"
            ? "Current"
            : effectiveCopy.state === "stale"
              ? "Stale"
              : "None"}
        </span>
      </div>

      {effectiveCopy.state === "current" && effectiveCopy.short_description && (draft === null ? (
        <div>
          <p className="current-copy-text">{effectiveCopy.short_description}</p>
          <button type="button" onClick={() => { setDraft(effectiveCopy.short_description); setValidation(null); }}>Edit</button>
        </div>
      ) : (
        <div className="current-copy-editor">
          <label htmlFor="current-copy-draft">Edit current description</label>
          <textarea id="current-copy-draft" maxLength={180} value={draft} disabled={saving} onChange={(event) => { setDraft(event.target.value); setValidation(null); }} />
          <span>{draft.length} / 180</span>
          {validation && <div role="alert">{validation}</div>}
          <div className="current-copy-editor-actions">
            <button type="button" disabled={saving} onClick={() => { setDraft(null); setValidation(null); }}>Cancel</button>
            <button type="button" disabled={saving} onClick={() => void save()}>{saving ? "Saving…" : "Save changes"}</button>
          </div>
        </div>
      ))}
      {effectiveCopy.state === "stale" && (
        <div className="editorial-copy-message stale-copy-message">
          <strong>Reviewed copy is stale.</strong>
          <span>The Product facts changed after it was approved, so it will not be published.</span>
        </div>
      )}
      {effectiveCopy.state === "none" && (
        <div className="editorial-copy-message">
          <strong>No approved Product description yet.</strong>
          <span>Generate a proposal, then approve or correct it after review.</span>
        </div>
      )}
    </section>
  );
}

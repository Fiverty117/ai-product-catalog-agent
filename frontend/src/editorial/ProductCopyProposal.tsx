import { useState } from "react";

import type { ProductCopyEditorialRun } from "../api";

type ReviewAction = (
  runId: string,
  decision: "approved" | "corrected" | "rejected",
  correctedText?: string,
) => Promise<void>;

function formatTimestamp(value: string | null): string {
  if (!value) return "In progress";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function ProductCopyProposal({
  run,
  busy,
  onReview,
}: {
  run: ProductCopyEditorialRun;
  busy: boolean;
  onReview: ReviewAction;
}) {
  const [correcting, setCorrecting] = useState(false);
  const [correction, setCorrection] = useState(run.generated_text ?? "");
  const [confirmingReject, setConfirmingReject] = useState(false);
  const canReview =
    run.status === "succeeded" &&
    run.review_state === "unreviewed" &&
    run.generated_text !== null;
  const validCorrection = correction.trim().length > 0 && correction.length <= 180;

  return (
    <article className="proposal-card">
      <div className="proposal-heading">
        <div className="proposal-badges">
          <span className={`proposal-status status-${run.status}`}>{titleCase(run.status)}</span>
          <span className={`proposal-status review-${run.review_state}`}>
            {run.review_state === "unreviewed" ? "Pending review" : titleCase(run.review_state)}
          </span>
          <span className={`source-state source-${run.source_state}`}>
            {run.source_state === "current" ? "Current facts" : "Stale facts"}
          </span>
        </div>
        <time dateTime={run.completed_at ?? run.created_at}>
          {formatTimestamp(run.completed_at ?? run.created_at)}
        </time>
      </div>

      {run.generated_text && <p className="proposal-text">{run.generated_text}</p>}
      {run.status === "running" && (
        <p className="proposal-message">The proposal is being generated.</p>
      )}
      {run.status === "failed" && (
        <p className="proposal-error">{run.sanitized_error ?? "Generation failed."}</p>
      )}
      {run.review?.decision === "corrected" && run.review.corrected_short_description && (
        <div className="corrected-result">
          <strong>Human correction</strong>
          <p>{run.review.corrected_short_description}</p>
        </div>
      )}

      {canReview && !correcting && !confirmingReject && (
        <div className="proposal-actions">
          <button
            type="button"
            className="primary-action"
            disabled={busy}
            onClick={() => void onReview(run.run_id, "approved")}
          >
            Approve
          </button>
          <button type="button" disabled={busy} onClick={() => setCorrecting(true)}>
            Correct
          </button>
          <button type="button" disabled={busy} onClick={() => setConfirmingReject(true)}>
            Reject
          </button>
        </div>
      )}

      {canReview && correcting && (
        <div className="correction-form">
          <label>
            Corrected description
            <textarea
              value={correction}
              maxLength={180}
              rows={4}
              onChange={(event) => setCorrection(event.target.value)}
            />
          </label>
          <div className="correction-meta">
            <span className={!validCorrection ? "validation-text" : ""}>
              {correction.length} / 180 characters
            </span>
            <div className="proposal-actions">
              <button
                type="button"
                className="primary-action"
                disabled={busy || !validCorrection}
                onClick={() => void onReview(run.run_id, "corrected", correction)}
              >
                Save correction
              </button>
              <button type="button" disabled={busy} onClick={() => setCorrecting(false)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {canReview && confirmingReject && (
        <div className="reject-confirmation" role="alert">
          <span>Reject this proposal? It will remain in history.</span>
          <div className="proposal-actions">
            <button
              type="button"
              className="danger-action"
              disabled={busy}
              onClick={() => void onReview(run.run_id, "rejected")}
            >
              Confirm reject
            </button>
            <button type="button" disabled={busy} onClick={() => setConfirmingReject(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <details className="technical-details">
        <summary>Technical details</summary>
        <dl>
          <div><dt>Provider</dt><dd>{run.provider}</dd></div>
          <div><dt>Model</dt><dd>{run.model}</dd></div>
        </dl>
      </details>
    </article>
  );
}

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProductImageEditorialSummary, ProductSummary } from "../api";
import { ProductImageEditor } from "./ProductImageEditor";

const product: ProductSummary = {
  product_id: "product-1", product_name: "Whey", brand_name: "Landerfit",
  primary_category_id: null, primary_category_name: null,
  readiness: { ready: false, blockers: [], warnings: [], presentation_warnings: [] },
  publishable_skus: [], hero: null, copy_state: "none", short_description: null,
};

const summary: ProductImageEditorialSummary = {
  product_id: "product-1", source_photo_id: "photo-1", source_owner: "product", source_sku_id: null,
  original_preview_url: "/api/products/product-1/images/photos/photo-1",
  effective: { presentation: "derived", derived_image_id: "approved", preview_url: "/approved", warnings: [] },
  derived_images: [
    { derived_image_id: "approved", review_state: "approved", selectable: true, asset_available: true, selected: true, preview_url: "/approved", created_at: "2026-09-18T12:00:00Z" },
    { derived_image_id: "pending", review_state: "unreviewed", selectable: false, asset_available: true, selected: false, preview_url: "/pending", created_at: "2026-09-18T12:01:00Z" },
    { derived_image_id: "rejected", review_state: "rejected", selectable: false, asset_available: true, selected: false, preview_url: "/rejected", created_at: "2026-09-18T12:02:00Z" },
  ], generations: [], can_generate: true, product,
};

describe("ProductImageEditor", () => {
  it("shows current, original, approved, pending and rejected states and emits explicit actions", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(<ProductImageEditor summary={summary} busyAction={null} onAction={onAction} />);
    expect(screen.getByText("Using: Enhanced image")).toBeVisible();
    expect(screen.getByText("Original source image")).toBeVisible();
    expect(screen.getByRole("img", { name: "Selected source Photo" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Generate enhanced image" }));
    expect(onAction).toHaveBeenCalledWith({ kind: "generate" });
    expect(screen.getByText("Pending review")).toBeVisible();
    expect(screen.getAllByText("Rejected").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Rejected" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Use original" }));
    expect(onAction).toHaveBeenCalledWith({ kind: "select", derivedImageId: null });
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onAction).toHaveBeenCalledWith({ kind: "review", derivedImageId: "pending", decision: "approved" });
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(onAction).toHaveBeenCalledWith({ kind: "review", derivedImageId: "pending", decision: "rejected" });

    rerender(<ProductImageEditor summary={{ ...summary, derived_images: summary.derived_images.map((image) => image.derived_image_id === "pending" ? { ...image, review_state: "approved", selectable: true } : image) }} busyAction={null} onAction={onAction} />);
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();

    rerender(<ProductImageEditor summary={{ ...summary, effective: { ...summary.effective!, presentation: "original", derived_image_id: null }, derived_images: summary.derived_images.map((image) => image.derived_image_id === "approved" ? { ...image, selected: false } : image) }} busyAction={null} onAction={onAction} />);
    await user.click(screen.getByRole("button", { name: "Use enhanced image" }));
    expect(onAction).toHaveBeenCalledWith({ kind: "select", derivedImageId: "approved" });
  });

  it("shows saving state and disables duplicate actions", () => {
    render(<ProductImageEditor summary={{ ...summary, effective: { ...summary.effective!, presentation: "original", derived_image_id: null }, derived_images: summary.derived_images.map((image) => image.derived_image_id === "approved" ? { ...image, selected: false } : image) }} busyAction="select-approved" onAction={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  it("shows queued, running, failed, and retry states without exposing selection of pending images", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn().mockResolvedValue(undefined);
    const job = { job_id: "job-1", status: "queued" as const, attempts: 0, max_attempts: 3, can_retry: false, created_at: "2026-09-18T12:00:00Z" };
    const { rerender } = render(<ProductImageEditor summary={{ ...summary, can_generate: false, generations: [job] }} busyAction={null} onAction={onAction} />);
    expect(screen.getByText("Queued")).toBeVisible();
    expect(screen.getByRole("button", { name: "Enhancing image…" })).toBeDisabled();
    rerender(<ProductImageEditor summary={{ ...summary, can_generate: false, generations: [{ ...job, status: "running" }] }} busyAction={null} onAction={onAction} />);
    expect(screen.getAllByText("Enhancing image…").length).toBeGreaterThan(0);
    rerender(<ProductImageEditor summary={{ ...summary, generations: [{ ...job, status: "failed", can_retry: true }] }} busyAction={null} onAction={onAction} />);
    await user.click(screen.getByRole("button", { name: "Retry enhancement" }));
    expect(onAction).toHaveBeenCalledWith({ kind: "retry", jobId: "job-1" });
  });
});

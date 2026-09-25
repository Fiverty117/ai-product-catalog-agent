import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import type { IntakeItem, IntakePromotionResult } from "./api";

const api = vi.hoisted(() => ({
  fetchIntakeItems: vi.fn(), fetchIntakeItem: vi.fn(), createIntakeItem: vi.fn(),
  saveIntakeDraft: vi.fn(), runIntakeExtraction: vi.fn(), setIntakePrimaryPhoto: vi.fn(),
  fetchIntakePromotion: vi.fn(), createIntakePromotion: vi.fn(),
}));
vi.mock("./api", async () => ({ ...await vi.importActual<typeof import("./api")>("./api"), ...api }));
vi.mock("./CatalogBuilderPage", async () => {
  const { useLocation } = await import("react-router-dom");
  return { CatalogBuilderPage: () => <div>Catalog Builder destination: {useLocation().search}</div> };
});

import { App } from "./App";

const base: IntakeItem = {
  id: "item-1", status: "draft", created_at: "2026-09-23T12:00:00Z", updated_at: "2026-09-23T12:00:00Z",
  draft: { schema_version: 1, brand_name: null, product_name: null, primary_category_name: null,
    secondary_category_names: [], skus: [], notes: null }, human_edited: false,
  photos: [{ id: "photo-1", position: 0, is_primary: true, original_filename: "front.png", mime_type: "image/png", image_url: "/api/product-intake/items/item-1/photos/photo-1/image" }],
  extraction: { job_id: null, job_status: null, attempts: null, max_attempts: null,
    run_id: null, run_status: null, error: null, newer_result_available: false, observation: null },
  promotion: null,
};

const eligible: IntakeItem = {
  ...base,
  status: "review_required",
  draft: { ...base.draft, brand_name: "Grabelan", product_name: "Matcha", primary_category_name: "Tea suggestion", skus: [
    { flavor: "Plain", size_value: "1000", size_unit: "g", servings: 30, external_sku: "M-1" },
    { flavor: "Berry", size_value: "1", size_unit: "kg", servings: 60, external_sku: "M-2" },
  ] },
};

const promoted: IntakePromotionResult = {
  status: "promoted", readiness_currency: "PYG", intake_id: "item-1", product_id: "11111111-1111-4111-8111-111111111111",
  brand_id: "brand-1", brand_name: "Grabelan", brand_reused: true,
  promoted_at: "2026-09-23T12:00:00Z", sku_ids: ["sku-1", "sku-2"],
  product: { product_id: "11111111-1111-4111-8111-111111111111", product_name: "Matcha", brand_name: "Grabelan",
    primary_category_id: "cat-1", primary_category_name: "Tea", readiness: { ready: true, blockers: [], warnings: [], presentation_warnings: [] },
    publishable_skus: [], hero: null, copy_state: "none", short_description: null },
};

function mount(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchIntakeItems.mockResolvedValue({ items: [] });
  api.fetchIntakeItem.mockResolvedValue(base);
  api.createIntakeItem.mockResolvedValue(base);
  api.saveIntakeDraft.mockImplementation(async (_id, draft) => ({ ...base, draft }));
  api.runIntakeExtraction.mockResolvedValue({ ...base, status: "queued" });
  api.fetchIntakePromotion.mockResolvedValue({ categories: [], result: null });
  api.createIntakePromotion.mockResolvedValue(promoted);
});

describe("Product Intake workspace", () => {
  it("shows empty state, multi-file upload and opens the new item", async () => {
    const user = userEvent.setup();
    mount("/product-intake");
    expect(await screen.findByText("No products in intake yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Catalog Builder" })).toHaveAttribute("href", "/catalog-builder");
    await user.click(screen.getByRole("button", { name: "Add product" }));
    const picker = screen.getByLabelText("Source photos");
    await user.upload(picker, [new File(["a"], "a.png", { type: "image/png" }), new File(["b"], "b.png", { type: "image/png" })]);
    expect(screen.getByText("a.png")).toBeInTheDocument();
    expect(screen.getByText("b.png")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(api.createIntakeItem).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ name: "a.png" }), expect.objectContaining({ name: "b.png" })])));
    expect(await screen.findByText("Source photos", { selector: "h2" })).toBeInTheDocument();
    expect(screen.getByAltText("front.png")).toHaveAttribute("src", base.photos[0].image_url);
  });

  it("edits and cancels draft locally, then saves typed variants", async () => {
    const user = userEvent.setup();
    mount("/product-intake/item-1");
    const brand = await screen.findByLabelText("Brand");
    await user.type(brand, "Edited Brand");
    await user.click(screen.getByRole("button", { name: "Add draft variant" }));
    await user.type(screen.getByLabelText("Flavor"), "Vanilla");
    await user.type(screen.getByLabelText("Size"), "750");
    await user.type(screen.getByLabelText("Unit"), "g");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText("Brand")).toHaveValue("");
    expect(screen.queryByLabelText("Flavor")).not.toBeInTheDocument();
    expect(api.saveIntakeDraft).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("Brand"), "Edited Brand");
    await user.click(screen.getByRole("button", { name: "Add draft variant" }));
    await user.type(screen.getByLabelText("Flavor"), "Vanilla");
    await user.type(screen.getByLabelText("Size"), "750");
    await user.type(screen.getByLabelText("Unit"), "g");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.saveIntakeDraft).toHaveBeenCalledWith("item-1", expect.objectContaining({ brand_name: "Edited Brand", skus: [expect.objectContaining({ flavor: "Vanilla", size_value: "750", size_unit: "g" })] })));
    expect(screen.getByRole("button", { name: "Create product" })).toBeDisabled();
    expect(screen.getByText("Complete Brand, Product name and at least one variant to continue.")).toBeInTheDocument();
  });

  it("runs extraction explicitly and shows persisted completion or failure", async () => {
    const user = userEvent.setup();
    const view = mount("/product-intake/item-1");
    await screen.findByText("Source photos", { selector: "h2" });
    await user.click(screen.getByRole("button", { name: "Run extraction" }));
    await waitFor(() => expect(api.runIntakeExtraction).toHaveBeenCalledWith("item-1", expect.any(String)));
    expect(screen.getByText("Queued", { selector: "strong" })).toBeInTheDocument();
    api.fetchIntakeItem.mockResolvedValue({ ...base, status: "review_required", draft: { ...base.draft, brand_name: "Observed" }, extraction: { ...base.extraction, run_status: "succeeded" } });
    view.unmount();
    mount("/product-intake/item-1");
    expect(await screen.findByDisplayValue("Observed")).toBeInTheDocument();
    expect(screen.getByText("Review required", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create product" })).toBeDisabled();
  });

  it("shows a failed extraction from backend truth", async () => {
    api.fetchIntakeItem.mockResolvedValue({ ...base, status: "failed", extraction: { ...base.extraction, error: "Extraction failed.", run_status: "failed" } });
    mount("/product-intake/item-1");
    expect(await screen.findByText("Extraction failed.")).toBeInTheDocument();
    expect(screen.getByText("Failed", { selector: "strong" })).toBeInTheDocument();
  });

  it("requires a visible review panel and explicit category/price choices", async () => {
    const user = userEvent.setup();
    api.fetchIntakeItem.mockResolvedValue(eligible);
    api.fetchIntakePromotion.mockResolvedValue({ categories: [{ id: "cat-1", name: "Tea" }, { id: "cat-2", name: "Supplements" }], result: null });
    mount("/product-intake/item-1");
    const create = await screen.findByRole("button", { name: "Create product" });
    expect(create).toBeEnabled();
    await user.click(create);
    expect(api.createIntakePromotion).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Manage categories" })).toHaveAttribute("target", "_blank");
    expect(api.fetchIntakePromotion).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Tea suggestion", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Plain · 1000 g · M-1")).toBeInTheDocument();
    expect(screen.getByText("Berry · 1 kg · M-2")).toBeInTheDocument();
    expect(screen.getAllByText("No price — Product may not be catalog-ready.")).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("Canonical primary Category"), "cat-1");
    await user.click(screen.getByRole("checkbox", { name: "Supplements" }));
    await user.type(screen.getByLabelText("Price for variant 1"), "125000.50");
    await user.click(within(screen.getByLabelText("Review canonical product promotion")).getByRole("button", { name: "Cancel" }));
    expect(api.createIntakePromotion).not.toHaveBeenCalled();
  });

  it("submits once, shows readiness and navigates to canonical Product", async () => {
    const user = userEvent.setup();
    api.fetchIntakeItem.mockResolvedValue(eligible);
    api.fetchIntakePromotion.mockResolvedValue({ categories: [{ id: "cat-1", name: "Tea" }], result: null });
    let finish!: (value: IntakePromotionResult) => void;
    api.createIntakePromotion.mockImplementation(() => new Promise<IntakePromotionResult>((resolve) => { finish = resolve; }));
    mount("/product-intake/item-1");
    await user.click(await screen.findByRole("button", { name: "Create product" }));
    await user.selectOptions(screen.getByLabelText("Canonical primary Category"), "cat-1");
    await user.type(screen.getByLabelText("Price for variant 1"), "125000.50");
    const confirm = screen.getByRole("button", { name: "Create canonical product" });
    await user.dblClick(confirm);
    await waitFor(() => expect(api.createIntakePromotion).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "Creating product..." })).toBeDisabled();
    expect(api.createIntakePromotion).toHaveBeenCalledWith("item-1", expect.objectContaining({
      primary_category_id: "cat-1", sku_prices: [{ intake_sku_index: 0, amount: "125000.50", currency: "PYG" }],
    }));
    finish(promoted);
    expect(await screen.findByText("Ready for catalog (PYG)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open product" }));
    expect(await screen.findByText(/Catalog Builder destination: \?product=11111111/)).toBeInTheDocument();
  });

  it("shows identity conflict and preserves the unpromoted intake", async () => {
    const user = userEvent.setup();
    api.fetchIntakeItem.mockResolvedValue(eligible);
    api.createIntakePromotion.mockRejectedValue(new ApiError(409, "A canonical Product with this identity already exists: Grabelan / Matcha."));
    mount("/product-intake/item-1");
    await user.click(await screen.findByRole("button", { name: "Create product" }));
    await user.click(screen.getByRole("button", { name: "Create canonical product" }));
    expect(await screen.findByText(/canonical Product with this identity already exists/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create product" })).toBeEnabled();
  });

  it("restores promoted linkage and blocks a second promotion", async () => {
    api.fetchIntakeItem.mockResolvedValue({ ...eligible, status: "promoted", promotion: { product_id: promoted.product_id, promoted_at: promoted.promoted_at } });
    api.fetchIntakePromotion.mockResolvedValue({ categories: [], result: { ...promoted, product: { ...promoted.product, readiness: { ...promoted.product.readiness, ready: false, blockers: [{ code: "no_publishable_skus", severity: "blocker", scope: "product", message: "No approved Price.", product_id: promoted.product_id, sku_id: null, category_id: null, photo_id: null }] } } } });
    mount("/product-intake/item-1");
    expect(await screen.findByText("Not ready for catalog (PYG)")).toBeInTheDocument();
    expect(screen.getByText("No approved Price.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create product" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Brand")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Run extraction" })).not.toBeInTheDocument();
  });
});

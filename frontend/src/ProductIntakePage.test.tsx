import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { IntakeItem } from "./api";

const api = vi.hoisted(() => ({
  fetchIntakeItems: vi.fn(), fetchIntakeItem: vi.fn(), createIntakeItem: vi.fn(),
  saveIntakeDraft: vi.fn(), runIntakeExtraction: vi.fn(), setIntakePrimaryPhoto: vi.fn(),
}));
vi.mock("./api", async () => ({ ...await vi.importActual<typeof import("./api")>("./api"), ...api }));

import { App } from "./App";

const base: IntakeItem = {
  id: "item-1", status: "draft", created_at: "2026-09-23T12:00:00Z", updated_at: "2026-09-23T12:00:00Z",
  draft: { schema_version: 1, brand_name: null, product_name: null, primary_category_name: null,
    secondary_category_names: [], skus: [], notes: null }, human_edited: false,
  photos: [{ id: "photo-1", position: 0, is_primary: true, original_filename: "front.png", mime_type: "image/png", image_url: "/api/product-intake/items/item-1/photos/photo-1/image" }],
  extraction: { job_id: null, job_status: null, attempts: null, max_attempts: null,
    run_id: null, run_status: null, error: null, newer_result_available: false, observation: null },
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
    expect(screen.getByText("Canonical product creation will be enabled in the next step.")).toBeInTheDocument();
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
});

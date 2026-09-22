import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ProductCopyEditorialRun,
  ProductCopyEditorialSummary,
  ProductSummary,
} from "../api";

const apiMocks = vi.hoisted(() => ({
  fetchProductCopyEditorial: vi.fn(),
  generateProductCopy: vi.fn(),
  retryProductCopyGeneration: vi.fn(),
  reviewProductCopy: vi.fn(),
  saveManualProductCopyRevision: vi.fn(),
  fetchProductData: vi.fn(),
  saveProductIdentity: vi.fn(),
  saveProductCategories: vi.fn(),
  saveProductSKU: vi.fn(),
  addProductSKU: vi.fn(),
  changeProductPrice: vi.fn(),
  fetchProductImageEditorial: vi.fn(),
  reviewProductDerivedImage: vi.fn(),
  selectProductImagePresentation: vi.fn(),
}));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, ...apiMocks };
});

import { ProductEditorialDrawer } from "./ProductEditorialDrawer";

const product: ProductSummary = {
  product_id: "product-1",
  product_name: "Premium Whey",
  brand_name: "Landerfit",
  primary_category_id: "category-1",
  primary_category_name: "Proteínas",
  readiness: {
    ready: true,
    blockers: [],
    warnings: [],
    presentation_warnings: [],
  },
  publishable_skus: [{
    sku_id: "sku-1",
    variant_label: "Vanilla / 2 LB",
    flavor: "Vanilla",
    size_value: "2",
    size_unit: "LB",
    active_price_amount: "360000.0000",
    currency: "PYG",
  }],
  hero: null,
  copy_state: "current",
  short_description: "Existing approved text.",
};

function run(overrides: Partial<ProductCopyEditorialRun> = {}): ProductCopyEditorialRun {
  return {
    run_id: "run-1",
    job_id: "job-1",
    status: "succeeded",
    review_state: "unreviewed",
    generated_text: "Alternative proposed text.",
    sanitized_error: null,
    source_state: "current",
    review: null,
    provider: "openai",
    model: "gpt-5.6-sol",
    created_at: "2026-09-18T12:00:00Z",
    completed_at: "2026-09-18T12:00:05Z",
    ...overrides,
  };
}

function summary(
  state: "current" | "stale" | "none" = "current",
  overrides: Partial<ProductCopyEditorialSummary> = {},
): ProductCopyEditorialSummary {
  return {
    product: {
      ...product,
      copy_state: state,
      short_description: state === "current" ? "Existing approved text." : null,
    },
    effective_copy: {
      state,
      short_description: state === "current" ? "Existing approved text." : null,
    },
    generations: [],
    manual_revisions: [],
    runs: [run()],
    has_active_generation: false,
    ...overrides,
  };
}

function renderDrawer(onClose = vi.fn(), onProductUpdated = vi.fn()) {
  render(
    <ProductEditorialDrawer
      productId="product-1"
      onClose={onClose}
      onProductUpdated={onProductUpdated}
    />,
  );
  return { onClose, onProductUpdated };
}

beforeEach(() => {
  apiMocks.selectProductImagePresentation.mockReset();
  apiMocks.reviewProductDerivedImage.mockReset();
  apiMocks.fetchProductImageEditorial.mockResolvedValue({
    product_id: "product-1", source_photo_id: null, source_owner: null,
    source_sku_id: null, original_preview_url: null, effective: null,
    derived_images: [], product,
  });
  apiMocks.fetchProductData.mockResolvedValue({
    product, brand_id: "brand-1", brands: [{ brand_id: "brand-1", name: "Landerfit" }],
    categories: [{ category_id: "category-1", name: "Proteínas", assigned: true, is_primary: true }],
    skus: [{ sku_id: "sku-1", flavor: "Vanilla", size_value: "2", size_unit: "LB", servings: 30, external_sku: null,
      active_price: { price_id: "price-1", amount: "360000.0000", currency: "PYG", valid_from: "2026-09-18T12:00:00Z", source: "human", approved: true }, price_history: [] }],
    identity_history: [],
  });
  apiMocks.saveManualProductCopyRevision.mockReset();
  apiMocks.fetchProductCopyEditorial.mockResolvedValue(summary());
  apiMocks.generateProductCopy.mockResolvedValue({
    generation: {
      job_id: "job-new",
      status: "queued",
      attempts: 0,
      max_attempts: 3,
      can_retry: false,
      error: null,
      created_at: "2026-09-18T12:01:00Z",
      updated_at: "2026-09-18T12:01:00Z",
      finished_at: null,
    },
    editorial: summary("current", {
      has_active_generation: true,
      generations: [{
        job_id: "job-new",
        status: "queued",
        attempts: 0,
        max_attempts: 3,
        can_retry: false,
        error: null,
        created_at: "2026-09-18T12:01:00Z",
        updated_at: "2026-09-18T12:01:00Z",
        finished_at: null,
      }],
    }),
  });
});

describe("ProductEditorialDrawer", () => {
  it("keeps image state visible and shows an error when selection fails", async () => {
    const imageSummary = {
      product_id: "product-1", source_photo_id: "photo-1", source_owner: "product" as const, source_sku_id: null,
      original_preview_url: "/original", effective: { presentation: "original" as const, derived_image_id: null, preview_url: "/original", warnings: [] },
      derived_images: [{ derived_image_id: "approved", review_state: "approved" as const, selectable: true, asset_available: true, selected: false, preview_url: "/approved", created_at: "2026-09-18T12:00:00Z" }], product,
    };
    apiMocks.fetchProductImageEditorial.mockResolvedValue(imageSummary);
    let rejectSelection!: (reason: Error) => void;
    apiMocks.selectProductImagePresentation.mockReturnValue(new Promise((_resolve, reject) => { rejectSelection = reject; }));
    const user = userEvent.setup();
    renderDrawer();
    await user.click(await screen.findByRole("button", { name: "Use enhanced image" }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    rejectSelection(new Error("conflict"));
    expect(await screen.findByRole("alert")).toHaveTextContent("The Product image action could not be saved.");
    expect(screen.getByText("Using: Original image")).toBeVisible();
  }, 15000);
  it("keeps the Product draft on backend conflict and prevents a duplicate save", async () => {
    let rejectSave!: (reason: Error) => void;
    apiMocks.saveProductIdentity.mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject; }));
    const user = userEvent.setup();
    renderDrawer();
    await user.click(await screen.findByRole("button", { name: "Edit Product" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    rejectSave(new Error("identity conflict"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Product data could not be saved or refreshed.");
    expect(screen.getByRole("textbox", { name: "Product name" })).toHaveValue("Premium Whey");
  });
  it("edits current copy with cancel, validation, and save", async () => {
    const user = userEvent.setup();
    const updated = summary("current", {
      product: { ...product, short_description: "Human copy." },
      effective_copy: { state: "current", short_description: "Human copy." },
      manual_revisions: [{ revision_id: "revision-1", short_description: "Human copy.", source_state: "current", created_at: "2026-09-18T13:00:00Z" }],
    });
    apiMocks.saveManualProductCopyRevision.mockResolvedValue({ editorial: updated });
    const { onProductUpdated } = renderDrawer();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const editor = screen.getByRole("textbox", { name: "Edit current description" });
    expect(editor).toHaveValue("Existing approved text.");
    expect(screen.getByText("23 / 180")).toBeVisible();
    await user.clear(editor);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByText("Description must contain 1 to 180 characters.")).toBeVisible();
    expect(apiMocks.saveManualProductCopyRevision).not.toHaveBeenCalled();
    await user.type(editor, "Discard me");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("Existing approved text.", { selector: ".current-copy-text" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.clear(screen.getByRole("textbox", { name: "Edit current description" }));
    await user.type(screen.getByRole("textbox", { name: "Edit current description" }), "Human copy.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(apiMocks.saveManualProductCopyRevision).toHaveBeenCalledWith("product-1", "Human copy."));
    expect(await screen.findByText("Human copy.", { selector: ".current-copy-text" })).toBeVisible();
    expect(onProductUpdated).toHaveBeenLastCalledWith(updated.product);
    expect(apiMocks.generateProductCopy).not.toHaveBeenCalled();
    expect(screen.getByRole("region", { name: "Human edit history" })).toHaveTextContent("Human edit");
  });
  it.each([
    ["current", "Existing approved text."],
    ["stale", "Reviewed copy is stale."],
    ["none", "No approved Product description yet."],
  ] as const)("renders the %s effective-copy state", async (state, expected) => {
    apiMocks.fetchProductCopyEditorial.mockResolvedValue(summary(state));
    renderDrawer();
    expect(await screen.findByText(expected)).toBeVisible();
    if (state !== "current") expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("disables duplicate saves and shows a server error without losing the draft", async () => {
    let rejectSave!: (reason: Error) => void;
    apiMocks.saveManualProductCopyRevision.mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject; }));
    const user = userEvent.setup();
    renderDrawer();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    rejectSave(new Error("Server unavailable"));
    expect(await screen.findByRole("alert")).toHaveTextContent("The manual edit could not be saved.");
    expect(screen.getByRole("textbox", { name: "Edit current description" })).toHaveValue("Existing approved text.");
  });

  it("shows proposal history, technical details, and closes explicitly", async () => {
    const onClose = vi.fn();
    renderDrawer(onClose);

    expect(await screen.findByText("Alternative proposed text.")).toBeVisible();
    expect(screen.getByText("Pending review")).toBeVisible();
    expect(screen.queryByText("gpt-5.6-sol")).not.toBeVisible();
    await userEvent.click(screen.getByText("Technical details"));
    expect(screen.getByText("gpt-5.6-sol")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Close editorial workspace" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("keeps current copy visible while a new generation is requested", async () => {
    let resolveGeneration!: (value: Awaited<ReturnType<typeof import("../api")["generateProductCopy"]>>) => void;
    apiMocks.generateProductCopy.mockReturnValue(
      new Promise((resolve) => {
        resolveGeneration = resolve;
      }),
    );
    const user = userEvent.setup();
    renderDrawer();

    const generate = await screen.findByRole("button", { name: "Generate proposal" });
    await user.click(generate);
    expect(screen.getByText("Existing approved text.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Generating…" })).toBeDisabled();

    resolveGeneration({
      generation: {
        job_id: "job-new",
        status: "queued",
        attempts: 0,
        max_attempts: 3,
        can_retry: false,
        error: null,
        created_at: "2026-09-18T12:01:00Z",
        updated_at: "2026-09-18T12:01:00Z",
        finished_at: null,
      },
      editorial: summary("current", {
        has_active_generation: true,
        generations: [{
          job_id: "job-new",
          status: "queued",
          attempts: 0,
          max_attempts: 3,
          can_retry: false,
          error: null,
          created_at: "2026-09-18T12:01:00Z",
          updated_at: "2026-09-18T12:01:00Z",
          finished_at: null,
        }],
      }),
    });
    expect(await screen.findByText("Product Copy generation is in progress.")).toBeVisible();
    expect(screen.getByText("Existing approved text.")).toBeVisible();
  });

  it("polls while generation is active and stops after the proposal succeeds", async () => {
    vi.useFakeTimers();
    const active = summary("current", {
      has_active_generation: true,
      runs: [],
      generations: [{
        job_id: "job-active",
        status: "running",
        attempts: 1,
        max_attempts: 3,
        can_retry: false,
        error: null,
        created_at: "2026-09-18T12:01:00Z",
        updated_at: "2026-09-18T12:01:01Z",
        finished_at: null,
      }],
    });
    const completed = summary("current", {
      generations: [{
        ...active.generations[0],
        status: "succeeded",
        updated_at: "2026-09-18T12:01:04Z",
        finished_at: "2026-09-18T12:01:04Z",
      }],
      runs: [run({ generated_text: "Freshly completed proposal." })],
    });
    apiMocks.fetchProductCopyEditorial
      .mockResolvedValueOnce(active)
      .mockResolvedValueOnce(completed);

    try {
      renderDrawer();
      await act(async () => undefined);
      expect(screen.getByText("Product Copy generation is in progress.")).toBeVisible();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(screen.getByText("Freshly completed proposal.")).toBeVisible();
      expect(apiMocks.fetchProductCopyEditorial).toHaveBeenCalledTimes(2);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000);
      });
      expect(apiMocks.fetchProductCopyEditorial).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces failed generation and supports an explicit retry", async () => {
    const failed = summary("current", {
      generations: [{
        job_id: "job-failed",
        status: "failed",
        attempts: 1,
        max_attempts: 3,
        can_retry: true,
        error: "OpenAI Product copy request was rejected",
        created_at: "2026-09-18T12:00:00Z",
        updated_at: "2026-09-18T12:00:05Z",
        finished_at: "2026-09-18T12:00:05Z",
      }],
    });
    apiMocks.fetchProductCopyEditorial.mockResolvedValue(failed);
    apiMocks.retryProductCopyGeneration.mockResolvedValue({
      generation: { ...failed.generations[0], status: "queued", can_retry: false },
      editorial: summary("current", { has_active_generation: true }),
    });
    const user = userEvent.setup();
    renderDrawer();

    expect(await screen.findByText("Generation failed")).toBeVisible();
    expect(screen.getByText("OpenAI Product copy request was rejected")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry generation" }));
    expect(apiMocks.retryProductCopyGeneration).toHaveBeenCalledWith("product-1", "job-failed");
  });

  it("approves a proposal and removes one-time review actions", async () => {
    const reviewedSummary = summary("current", {
      product: { ...product, short_description: "Alternative proposed text." },
      effective_copy: { state: "current", short_description: "Alternative proposed text." },
      runs: [run({
        review_state: "approved",
        review: {
          decision: "approved",
          corrected_short_description: null,
          created_at: "2026-09-18T12:02:00Z",
        },
      })],
    });
    apiMocks.reviewProductCopy.mockResolvedValue({
      review: reviewedSummary.runs[0].review,
      editorial: reviewedSummary,
    });
    const user = userEvent.setup();
    const { onProductUpdated } = renderDrawer();

    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByText("Alternative proposed text.", { selector: ".current-copy-text" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(onProductUpdated).toHaveBeenLastCalledWith(reviewedSummary.product);
  });

  it("corrects with character feedback and confirms rejection", async () => {
    const user = userEvent.setup();
    const correctedSummary = summary("current", {
      effective_copy: { state: "current", short_description: "x".repeat(180) },
      runs: [run({
        review_state: "corrected",
        review: {
          decision: "corrected",
          corrected_short_description: "x".repeat(180),
          created_at: "2026-09-18T12:02:00Z",
        },
      })],
    });
    apiMocks.reviewProductCopy.mockResolvedValue({
      review: correctedSummary.runs[0].review,
      editorial: correctedSummary,
    });
    const view = renderDrawer();

    await user.click(await screen.findByRole("button", { name: "Correct" }));
    const textbox = screen.getByRole("textbox", { name: "Corrected description" });
    await user.clear(textbox);
    await user.type(textbox, "x".repeat(180));
    expect(screen.getByText("180 / 180 characters")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    expect(apiMocks.reviewProductCopy).toHaveBeenCalledWith(
      "product-1",
      "run-1",
      "corrected",
      "x".repeat(180),
    );

    view.onClose.mockClear();
    apiMocks.fetchProductCopyEditorial.mockResolvedValue(summary());
    view.onClose();
  });

  it("requires confirmation before rejecting", async () => {
    const rejectedSummary = summary("none", {
      runs: [run({
        review_state: "rejected",
        review: {
          decision: "rejected",
          corrected_short_description: null,
          created_at: "2026-09-18T12:02:00Z",
        },
      })],
    });
    apiMocks.reviewProductCopy.mockResolvedValue({
      review: rejectedSummary.runs[0].review,
      editorial: rejectedSummary,
    });
    const user = userEvent.setup();
    renderDrawer();

    await user.click(await screen.findByRole("button", { name: "Reject" }));
    expect(screen.getByText("Reject this proposal? It will remain in history.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Confirm reject" }));
    await waitFor(() => expect(apiMocks.reviewProductCopy).toHaveBeenCalledWith(
      "product-1",
      "run-1",
      "rejected",
      undefined,
    ));
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });
});

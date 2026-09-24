import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BrandProfile,
  CatalogBuild,
  CatalogLayout,
  ProductCopyEditorialSummary,
  ProductSummary,
} from "./api";

const apiMocks = vi.hoisted(() => ({
  fetchProducts: vi.fn(),
  fetchBrandProfiles: vi.fn(),
  fetchLayouts: vi.fn(),
  fetchProductCopyEditorial: vi.fn(),
  generateProductCopy: vi.fn(),
  retryProductCopyGeneration: vi.fn(),
  reviewProductCopy: vi.fn(),
  saveManualProductCopyRevision: vi.fn(),
  fetchProductData: vi.fn(),
  changeProductPrice: vi.fn(),
  saveProductCategories: vi.fn(),
  fetchProductImageEditorial: vi.fn(),
  selectProductImagePresentation: vi.fn(),
  reviewProductDerivedImage: vi.fn(),
  createCatalogBuild: vi.fn(),
  fetchCatalogBuild: vi.fn(),
  retryCatalogBuild: vi.fn(),
}));

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, ...apiMocks };
});

import { CatalogBuilderPage } from "./CatalogBuilderPage";

const layouts: CatalogLayout[] = [
  { key: "classic", version: "1", display_label: "Classic", products_per_row: 2, page_size: "A4", orientation: "portrait" },
  { key: "dense", version: "1", display_label: "Dense", products_per_row: 3, page_size: "A4", orientation: "portrait" },
  { key: "compact", version: "1", display_label: "Compact", products_per_row: 4, page_size: "A4", orientation: "portrait" },
];

const profiles: BrandProfile[] = [
  { id: "brand-1", key: "grabelan", display_name: "Grabelan Natural Market", logo_url: null, primary_color: "#183D2F", accent_color: "#C89B3C" },
  { id: "brand-2", key: "secondary", display_name: "Secondary Brand", logo_url: null, primary_color: "#223344", accent_color: "#556677" },
];

function catalogBuild(
  status: CatalogBuild["status"],
  overrides: Partial<CatalogBuild> = {},
): CatalogBuild {
  return {
    id: "build-1",
    status,
    catalog_snapshot_id: "snapshot-1",
    product_count: 1,
    currency: "PYG",
    catalog_brand_profile_id: "brand-1",
    catalog_brand_key: "grabelan",
    catalog_brand_display_name: "Grabelan Natural Market",
    layout_key: "classic",
    layout_version: "1",
    layout_display_label: "Classic",
    created_at: "2026-09-22T12:00:00Z",
    error: null,
    can_retry: false,
    artifact: null,
    ...overrides,
  };
}

function product(
  id: string,
  name: string,
  ready: boolean,
  copyState: ProductSummary["copy_state"],
): ProductSummary {
  return {
    product_id: id,
    product_name: name,
    brand_name: "Landerfit",
    primary_category_id: ready ? "category-1" : null,
    primary_category_name: ready ? "Proteínas" : null,
    readiness: {
      ready,
      blockers: ready ? [] : [{
        code: "missing_primary_category",
        severity: "blocker",
        scope: "product",
        message: "Product has no primary Category.",
        product_id: id,
        sku_id: null,
        category_id: null,
        photo_id: null,
      }],
      warnings: [],
      presentation_warnings: [],
    },
    publishable_skus: ready ? [{
      sku_id: `${id}-sku`,
      variant_label: "Vanilla / 2 LB",
      flavor: "Vanilla",
      size_value: "2",
      size_unit: "LB",
      active_price_amount: "350000.0000",
      currency: "PYG",
    }] : [],
    hero: ready ? {
      source_photo_id: `${id}-photo`,
      effective_derived_image_id: null,
      presentation_type: "original",
      image_url: `/api/catalog-builder/products/${id}/image`,
    } : null,
    copy_state: copyState,
    short_description: copyState === "current" ? "Approved short description." : null,
  };
}

const allProducts = [
  product("ready", "Premium Whey", true, "current"),
  product("stale", "Older Copy", true, "stale"),
  product("blocked", "Needs Review", false, "none"),
];

beforeEach(() => {
  window.history.replaceState({}, "", "/catalog-builder");
  apiMocks.fetchProductImageEditorial.mockImplementation((id: string) => Promise.resolve({
    product_id: id, source_photo_id: null, source_owner: null, source_sku_id: null,
    original_preview_url: null, effective: null, derived_images: [],
    product: allProducts.find((item) => item.product_id === id) ?? allProducts[0],
  }));
  apiMocks.fetchProductData.mockImplementation((id: string) => Promise.resolve({
    product: allProducts.find((item) => item.product_id === id) ?? allProducts[0],
    brand_id: "brand-1", brands: [{ brand_id: "brand-1", name: "Landerfit" }], categories: [], skus: [], identity_history: [],
  }));
  apiMocks.fetchBrandProfiles.mockResolvedValue(profiles);
  apiMocks.fetchLayouts.mockResolvedValue(layouts);
  apiMocks.fetchProducts.mockImplementation((search: string) => Promise.resolve({
    currency: "PYG",
    as_of: "2026-09-17T12:00:00Z",
    products: search ? allProducts.filter((item) => item.product_name.toLowerCase().includes(search.toLowerCase())) : allProducts,
  }));
  apiMocks.fetchProductCopyEditorial.mockReset();
  apiMocks.generateProductCopy.mockReset();
  apiMocks.retryProductCopyGeneration.mockReset();
  apiMocks.reviewProductCopy.mockReset();
  apiMocks.createCatalogBuild.mockReset();
  apiMocks.fetchCatalogBuild.mockReset();
  apiMocks.retryCatalogBuild.mockReset();
});

describe("CatalogBuilderPage", () => {
  it("opens the existing Product editorial drawer from a promoted Intake link", async () => {
    const productId = "11111111-1111-4111-8111-111111111111";
    window.history.replaceState({}, "", `/catalog-builder?product=${productId}`);
    apiMocks.fetchProductCopyEditorial.mockRejectedValue(new Error("fixture unavailable"));
    render(<CatalogBuilderPage />);
    await waitFor(() => expect(apiMocks.fetchProductCopyEditorial).toHaveBeenCalledWith(productId, expect.anything()));
  });

  it("shows an explicit initial loading state", () => {
    apiMocks.fetchProducts.mockReturnValue(new Promise(() => undefined));
    render(<CatalogBuilderPage />);
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("Loading Catalog Builder");
  });

  it("renders authoritative states and preserves selection across filters and configuration", async () => {
    const user = userEvent.setup();
    render(<CatalogBuilderPage />);

    const readyHeading = await screen.findByRole("heading", { name: "Premium Whey" });
    const readyCard = readyHeading.closest("article")!;
    const staleCard = screen.getByRole("heading", { name: "Older Copy" }).closest("article")!;
    const blockedCard = screen.getByRole("heading", { name: "Needs Review" }).closest("article")!;

    expect(within(readyCard).getByRole("checkbox")).toBeEnabled();
    expect(within(blockedCard).getByRole("checkbox")).toBeDisabled();
    expect(within(blockedCard).getByText("Product has no primary Category.")).toBeVisible();
    expect(within(readyCard).getByText("Copy: Current")).toBeVisible();
    expect(within(staleCard).getByText("Copy: Stale")).toBeVisible();
    expect(within(blockedCard).getByText("Copy: None")).toBeVisible();
    expect(within(readyCard).getByText("Vanilla / 2 LB")).toBeVisible();
    expect(within(readyCard).getByText("Gs. 350.000")).toBeVisible();

    await user.click(within(readyCard).getByRole("checkbox"));
    expect(screen.getByText("Products selected").nextElementSibling).toHaveTextContent("1");
    expect(screen.getByText("All selected Products ready").nextElementSibling).toHaveTextContent("Yes");

    await user.type(screen.getByRole("searchbox", { name: "Search products or brands" }), "Older");
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Premium Whey" })).not.toBeInTheDocument());
    await user.clear(screen.getByRole("searchbox", { name: "Search products or brands" }));
    const returnedCard = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    expect(within(returnedCard).getByRole("checkbox")).toBeChecked();

    await user.selectOptions(screen.getByRole("combobox", { name: "Catalog brand" }), "brand-2");
    await user.selectOptions(screen.getByRole("combobox", { name: "Layout" }), "dense");
    const summary = screen.getByRole("complementary", { name: "Catalog summary" });
    expect(within(summary).getByText("Brand").nextElementSibling).toHaveTextContent("Secondary Brand");
    expect(within(summary).getByText("Layout").nextElementSibling).toHaveTextContent("Dense");
    expect(within(summary).getByText("Columns").nextElementSibling).toHaveTextContent("3");
    expect(within(returnedCard).getByRole("checkbox")).toBeChecked();
    expect(screen.getByRole("button", { name: "Create catalog" })).toBeEnabled();
  }, 15000);

  it("creates once with stable choices, shows creating/queued state, and preserves Builder controls", async () => {
    const user = userEvent.setup();
    let resolveBuild!: (value: CatalogBuild) => void;
    apiMocks.createCatalogBuild.mockReturnValue(new Promise<CatalogBuild>((resolve) => { resolveBuild = resolve; }));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    const create = screen.getByRole("button", { name: "Create catalog" });
    expect(create).toBeDisabled();
    await user.click(within(card).getByRole("checkbox"));
    await user.selectOptions(screen.getByRole("combobox", { name: "Catalog brand" }), "brand-2");
    await user.selectOptions(screen.getByRole("combobox", { name: "Layout" }), "dense");
    expect(create).toBeEnabled();

    await user.dblClick(create);
    expect(apiMocks.createCatalogBuild).toHaveBeenCalledTimes(1);
    expect(apiMocks.createCatalogBuild).toHaveBeenCalledWith(expect.objectContaining({
      product_ids: ["ready"],
      catalog_brand_profile_id: "brand-2",
      layout_key: "dense",
      layout_version: "1",
      currency: "PYG",
      idempotency_key: expect.any(String),
    }));
    expect(screen.getByRole("button", { name: "Creating snapshot…" })).toBeDisabled();
    await act(async () => resolveBuild(catalogBuild("queued", {
      catalog_brand_profile_id: "brand-2",
      catalog_brand_key: "secondary",
      catalog_brand_display_name: "Secondary Brand",
      layout_key: "dense",
      layout_display_label: "Dense",
    })));
    expect(await screen.findByRole("heading", { name: "Catalog queued" })).toBeVisible();
    expect(within(card).getByRole("checkbox")).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Catalog brand" })).toHaveValue("brand-2");
    expect(screen.getByRole("combobox", { name: "Layout" })).toHaveValue("dense");
    await user.selectOptions(screen.getByRole("combobox", { name: "Catalog brand" }), "brand-1");
    await user.selectOptions(screen.getByRole("combobox", { name: "Layout" }), "classic");
    expect(screen.getByText(/1 Product · Secondary Brand · Dense/)).toBeVisible();
  });

  it("restores an identified build after refresh", async () => {
    const buildId = "b3226544-d364-4214-81df-1f9936dfe635";
    window.history.replaceState({}, "", `/catalog-builder?build=${buildId}`);
    apiMocks.fetchCatalogBuild.mockResolvedValue(catalogBuild("succeeded", {
      id: buildId,
      artifact: {
        id: "artifact-1",
        created_at: "2026-09-22T12:00:03Z",
        page_count: 1,
        preview_url: "/api/catalog-builder/artifacts/artifact-1/pdf",
        download_url: "/api/catalog-builder/artifacts/artifact-1/pdf?download=true",
      },
    }));
    render(<CatalogBuilderPage />);
    expect(await screen.findByRole("heading", { name: "Catalog ready" })).toBeVisible();
    expect(apiMocks.fetchCatalogBuild).toHaveBeenCalledWith(buildId, expect.any(AbortSignal));
  });

  it("reuses a pending key for the same choices after a lost response", async () => {
    const user = userEvent.setup();
    apiMocks.createCatalogBuild
      .mockRejectedValueOnce(new Error("Network unavailable"))
      .mockResolvedValueOnce(catalogBuild("queued"));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Create catalog" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Network unavailable");
    await user.click(screen.getByRole("button", { name: "Create catalog" }));
    expect(await screen.findByRole("heading", { name: "Catalog queued" })).toBeVisible();
    expect(apiMocks.createCatalogBuild).toHaveBeenCalledTimes(2);
    expect(apiMocks.createCatalogBuild.mock.calls[0][0].idempotency_key)
      .toBe(apiMocks.createCatalogBuild.mock.calls[1][0].idempotency_key);
  });

  it("does not create a second build from a fast double click", async () => {
    const user = userEvent.setup();
    apiMocks.createCatalogBuild.mockResolvedValue(catalogBuild("succeeded"));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.dblClick(screen.getByRole("button", { name: "Create catalog" }));
    expect(await screen.findByRole("heading", { name: "Catalog ready" })).toBeVisible();
    expect(apiMocks.createCatalogBuild).toHaveBeenCalledTimes(1);
  });

  it("polls only active builds and shows exact preview/download artifact actions", async () => {
    const user = userEvent.setup();
    apiMocks.createCatalogBuild.mockResolvedValue(catalogBuild("queued"));
    apiMocks.fetchCatalogBuild
      .mockResolvedValueOnce(catalogBuild("running"))
      .mockResolvedValueOnce(catalogBuild("succeeded", {
        artifact: {
          id: "artifact-1",
          created_at: "2026-09-22T12:00:03Z",
          page_count: 3,
          preview_url: "/api/catalog-builder/artifacts/artifact-1/pdf",
          download_url: "/api/catalog-builder/artifacts/artifact-1/pdf?download=true",
        },
      }));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Create catalog" }));
    expect(screen.getByRole("heading", { name: "Catalog queued" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Generating catalog…" }, { timeout: 3000 })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Catalog ready" }, { timeout: 3000 })).toBeVisible();
    expect(screen.getByText(/3 pages/)).toBeVisible();
    expect(screen.getByRole("link", { name: "Preview PDF" })).toHaveAttribute(
      "href", expect.stringContaining("/api/catalog-builder/artifacts/artifact-1/pdf"),
    );
    expect(screen.getByRole("link", { name: "Download PDF" })).toHaveAttribute(
      "href", expect.stringContaining("download=true"),
    );
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 2200)); });
    expect(apiMocks.fetchCatalogBuild).toHaveBeenCalledTimes(2);
  }, 12000);

  it("pauses polling after the bounded wait for a stalled build", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(0);
    const user = userEvent.setup();
    apiMocks.createCatalogBuild.mockResolvedValue(catalogBuild("queued"));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Create catalog" }));
    expect(await screen.findByRole("heading", { name: "Catalog queued" })).toBeVisible();
    now.mockReturnValue(10 * 60 * 1000 + 1);
    expect(await screen.findByText(/Status updates paused/,{},{timeout: 3500})).toBeVisible();
    expect(apiMocks.fetchCatalogBuild).not.toHaveBeenCalled();
    now.mockRestore();
  }, 6000);

  it("shows a sanitized failure and retries the same build identity", async () => {
    const user = userEvent.setup();
    apiMocks.createCatalogBuild.mockResolvedValue(catalogBuild("failed", {
      error: "Chromium could not create the PDF.",
      can_retry: true,
    }));
    apiMocks.retryCatalogBuild.mockResolvedValue(catalogBuild("queued"));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Create catalog" }));
    expect(await screen.findByText("Chromium could not create the PDF.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry render" }));
    expect(apiMocks.retryCatalogBuild).toHaveBeenCalledWith("build-1");
    expect(await screen.findByRole("heading", { name: "Catalog queued" })).toBeVisible();
  });

  it("refreshes authoritative Products after a readiness conflict without clearing composition", async () => {
    const user = userEvent.setup();
    const ready = product("ready", "Premium Whey", true, "current");
    const blocked = {
      ...ready,
      readiness: {
        ...ready.readiness,
        ready: false,
        blockers: product("ready", "Premium Whey", false, "none").readiness.blockers,
      },
    };
    apiMocks.fetchProducts
      .mockResolvedValueOnce({ currency: "PYG", as_of: "2026-09-22T12:00:00Z", products: [ready] })
      .mockResolvedValue({ currency: "PYG", as_of: "2026-09-22T12:00:01Z", products: [blocked] });
    apiMocks.createCatalogBuild.mockRejectedValue(Object.assign(new Error("One or more selected Products are no longer ready."), {
      status: 409,
      detail: {
        code: "catalog_readiness_changed",
        message: "One or more selected Products are no longer ready.",
        products: [{ product_id: "ready", blockers: blocked.readiness.blockers }],
      },
    }));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.type(screen.getByRole("searchbox", { name: "Search products or brands" }), "Premium");
    await user.selectOptions(screen.getByRole("combobox", { name: "Layout" }), "dense");
    const callsBeforeCreate = apiMocks.fetchProducts.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Create catalog" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Needs attention: Premium Whey");
    await waitFor(() => expect(apiMocks.fetchProducts.mock.calls.length).toBeGreaterThan(callsBeforeCreate));
    expect(within(card).getByRole("checkbox")).toBeChecked();
    expect(screen.getByRole("searchbox", { name: "Search products or brands" })).toHaveValue("Premium");
    expect(screen.getByRole("combobox", { name: "Layout" })).toHaveValue("dense");
  });

  it("opens editorial review, syncs approved copy, and preserves Builder selection", async () => {
    const user = userEvent.setup();
    const editorialProduct = product("editorial", "Editorial Whey", true, "none");
    const pendingSummary: ProductCopyEditorialSummary = {
      product: editorialProduct,
      effective_copy: { state: "none", short_description: null },
      generations: [],
      manual_revisions: [],
      runs: [{
        run_id: "run-editorial",
        job_id: "job-editorial",
        status: "succeeded",
        review_state: "unreviewed",
        generated_text: "Clean protein copy ready for approval.",
        sanitized_error: null,
        source_state: "current",
        review: null,
        provider: "openai",
        model: "gpt-5.6-sol",
        created_at: "2026-09-18T12:00:00Z",
        completed_at: "2026-09-18T12:00:05Z",
      }],
      has_active_generation: false,
    };
    const approvedProduct = {
      ...editorialProduct,
      copy_state: "current" as const,
      short_description: "Clean protein copy ready for approval.",
    };
    const approvedSummary: ProductCopyEditorialSummary = {
      ...pendingSummary,
      product: approvedProduct,
      effective_copy: {
        state: "current",
        short_description: "Clean protein copy ready for approval.",
      },
      runs: [{
        ...pendingSummary.runs[0],
        review_state: "approved",
        review: {
          decision: "approved",
          corrected_short_description: null,
          created_at: "2026-09-18T12:01:00Z",
        },
      }],
    };
    apiMocks.fetchProducts.mockResolvedValue({
      currency: "PYG",
      as_of: "2026-09-18T12:00:00Z",
      products: [editorialProduct],
    });
    apiMocks.fetchProductCopyEditorial.mockResolvedValue(pendingSummary);
    apiMocks.reviewProductCopy.mockResolvedValue({
      review: approvedSummary.runs[0].review,
      editorial: approvedSummary,
    });

    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Editorial Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.click(within(card).getByRole("button", { name: "Edit Product" }));
    expect(await screen.findByRole("dialog", { name: "Product data and copy" })).toBeVisible();
    expect(screen.getByText("No approved Product description yet.")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(
      "Clean protein copy ready for approval.",
      { selector: ".current-copy-text" },
    )).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Close editorial workspace" }));

    const updatedCard = screen.getByRole("heading", { name: "Editorial Whey" }).closest("article")!;
    expect(within(updatedCard).getByRole("checkbox")).toBeChecked();
    expect(within(updatedCard).getByText("Copy: Current")).toBeVisible();
    expect(within(updatedCard).getByText("Clean protein copy ready for approval.")).toBeVisible();

    const manualProduct = { ...approvedProduct, short_description: "Human wording." };
    apiMocks.fetchProductCopyEditorial.mockResolvedValue(approvedSummary);
    apiMocks.saveManualProductCopyRevision.mockResolvedValue({
      editorial: {
        ...approvedSummary,
        product: manualProduct,
        effective_copy: { state: "current", short_description: "Human wording." },
        manual_revisions: [{ revision_id: "manual-1", short_description: "Human wording.", source_state: "current", created_at: "2026-09-18T13:00:00Z" }],
      },
    });
    await user.click(within(updatedCard).getByRole("button", { name: "Edit Product" }));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const editBox = screen.getByRole("textbox", { name: "Edit current description" });
    await user.clear(editBox);
    await user.type(editBox, "Human wording.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Human wording.", { selector: ".current-copy-text" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Close editorial workspace" }));
    expect(within(updatedCard).getByRole("checkbox")).toBeChecked();
    expect(within(updatedCard).getByText("Human wording.")).toBeVisible();
  });

  it("refreshes price and readiness from the backend while retaining Builder composition", async () => {
    const user = userEvent.setup();
    const initial = product("ready", "Premium Whey", true, "current");
    const updated = { ...initial, publishable_skus: [{ ...initial.publishable_skus[0], active_price_amount: "375000.0000" }] };
    const editorial = (value: ProductSummary): ProductCopyEditorialSummary => ({
      product: value, effective_copy: { state: "current", short_description: value.short_description },
      generations: [], runs: [], manual_revisions: [], has_active_generation: false,
    });
    const data = (value: ProductSummary, amount: string) => ({
      product: value, brand_id: "brand-1", brands: [{ brand_id: "brand-1", name: "Landerfit" }], categories: [], identity_history: [],
      skus: [{ sku_id: "ready-sku", flavor: "Vanilla", size_value: "2", size_unit: "LB", servings: null, external_sku: null,
        active_price: { price_id: "price-1", amount, currency: "PYG", valid_from: "2026-09-18T12:00:00Z", source: "human", approved: true }, price_history: [] }],
    });
    apiMocks.fetchProducts.mockResolvedValue({ currency: "PYG", as_of: "2026-09-18T12:00:00Z", products: [initial] });
    apiMocks.fetchProductCopyEditorial.mockResolvedValueOnce(editorial(initial)).mockResolvedValue(editorial(updated));
    apiMocks.fetchProductData.mockResolvedValue(data(initial, "350000.0000"));
    apiMocks.changeProductPrice.mockResolvedValue(data(updated, "375000.0000"));
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.selectOptions(screen.getByRole("combobox", { name: "Layout" }), "dense");
    await user.click(within(card).getByRole("button", { name: "Edit Product" }));
    await user.click(await screen.findByRole("button", { name: "Change price" }));
    await user.clear(screen.getByRole("spinbutton", { name: "Price amount" }));
    await user.type(screen.getByRole("spinbutton", { name: "Price amount" }), "375000");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(apiMocks.changeProductPrice).toHaveBeenCalledWith("ready", "ready-sku", "375000", "PYG"));
    await user.click(screen.getByRole("button", { name: "Close editorial workspace" }));
    expect(within(card).getByRole("checkbox")).toBeChecked();
    expect(within(card).getByText("Gs. 375.000")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Layout" })).toHaveValue("dense");
  });

  it("keeps a selected Product removable when a Category edit makes it not ready", async () => {
    const user = userEvent.setup();
    const initial = product("ready", "Premium Whey", true, "current");
    const blocked = { ...initial, primary_category_id: null, primary_category_name: null,
      readiness: { ...initial.readiness, ready: false, blockers: product("x", "x", false, "none").readiness.blockers } };
    const editorial = (value: ProductSummary): ProductCopyEditorialSummary => ({
      product: value, effective_copy: { state: "current", short_description: value.short_description },
      generations: [], runs: [], manual_revisions: [], has_active_generation: false,
    });
    apiMocks.fetchProducts.mockResolvedValue({ currency: "PYG", as_of: "2026-09-18T12:00:00Z", products: [initial] });
    apiMocks.fetchProductCopyEditorial.mockResolvedValueOnce(editorial(initial)).mockResolvedValue(editorial(blocked));
    apiMocks.fetchProductData.mockResolvedValue({ product: initial, brand_id: "brand-1", brands: [],
      categories: [{ category_id: "cat-1", name: "Protein", assigned: true, is_primary: true }], skus: [], identity_history: [] });
    apiMocks.saveProductCategories.mockResolvedValue({ product: blocked, brand_id: "brand-1", brands: [],
      categories: [{ category_id: "cat-1", name: "Protein", assigned: false, is_primary: false }], skus: [], identity_history: [] });
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    await user.click(within(card).getByRole("button", { name: "Edit Product" }));
    await user.click(await screen.findByRole("button", { name: "Edit Categories" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Primary Category" }), "");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(within(card).getByText("Not ready")).toBeVisible());
    await user.click(screen.getByRole("button", { name: "Close editorial workspace" }));
    expect(within(card).getByRole("checkbox")).toBeChecked();
    expect(within(card).getByRole("checkbox")).toBeEnabled();
    expect(screen.getByText("All selected Products ready").nextElementSibling).toHaveTextContent("—");
    await user.click(within(card).getByRole("checkbox"));
    expect(within(card).getByRole("checkbox")).toBeDisabled();
  });

  it("switches the Builder card image after backend-confirmed presentation selection and preserves selection", async () => {
    const user = userEvent.setup();
    const initial = product("ready", "Premium Whey", true, "current");
    const updated = { ...initial, hero: { ...initial.hero!, effective_derived_image_id: "derived-1", presentation_type: "derived" as const } };
    apiMocks.fetchProducts.mockResolvedValue({ currency: "PYG", as_of: "2026-09-18T12:00:00Z", products: [initial] });
    apiMocks.fetchProductCopyEditorial.mockResolvedValue({ product: initial, effective_copy: { state: "current", short_description: initial.short_description }, generations: [], runs: [], manual_revisions: [], has_active_generation: false });
    apiMocks.fetchProductImageEditorial.mockResolvedValue({
      product_id: "ready", source_photo_id: "ready-photo", source_owner: "product", source_sku_id: null,
      original_preview_url: "/original", effective: { presentation: "original", derived_image_id: null, preview_url: "/original", warnings: [] },
      derived_images: [{ derived_image_id: "derived-1", review_state: "approved", selectable: true, asset_available: true, selected: false, preview_url: "/derived", created_at: "2026-09-18T12:00:00Z" }], product: initial,
    });
    apiMocks.selectProductImagePresentation.mockResolvedValue({
      product_id: "ready", source_photo_id: "ready-photo", source_owner: "product", source_sku_id: null,
      original_preview_url: "/original", effective: { presentation: "derived", derived_image_id: "derived-1", preview_url: "/derived", warnings: [] },
      derived_images: [{ derived_image_id: "derived-1", review_state: "approved", selectable: true, asset_available: true, selected: true, preview_url: "/derived", created_at: "2026-09-18T12:00:00Z" }], product: updated,
    });
    render(<CatalogBuilderPage />);
    const card = (await screen.findByRole("heading", { name: "Premium Whey" })).closest("article")!;
    await user.click(within(card).getByRole("checkbox"));
    const firstImage = within(card).getByRole("img");
    await user.click(within(card).getByRole("button", { name: "Edit Product" }));
    await user.click(await screen.findByRole("button", { name: "Use enhanced image" }));
    await waitFor(() => expect(apiMocks.selectProductImagePresentation).toHaveBeenCalledWith("ready", "ready-photo", "derived-1"));
    expect(within(card).getByRole("checkbox")).toBeChecked();
    expect(within(card).getByRole("img")).not.toBe(firstImage);
    expect(within(card).getByRole("img")).toHaveAttribute("src", expect.stringContaining("/api/catalog-builder/products/ready/image"));
  });
});

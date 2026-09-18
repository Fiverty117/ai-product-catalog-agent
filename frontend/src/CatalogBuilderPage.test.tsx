import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BrandProfile, CatalogLayout, ProductSummary } from "./api";

const apiMocks = vi.hoisted(() => ({
  fetchProducts: vi.fn(),
  fetchBrandProfiles: vi.fn(),
  fetchLayouts: vi.fn(),
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
  apiMocks.fetchBrandProfiles.mockResolvedValue(profiles);
  apiMocks.fetchLayouts.mockResolvedValue(layouts);
  apiMocks.fetchProducts.mockImplementation((search: string) => Promise.resolve({
    currency: "PYG",
    as_of: "2026-09-17T12:00:00Z",
    products: search ? allProducts.filter((item) => item.product_name.toLowerCase().includes(search.toLowerCase())) : allProducts,
  }));
});

describe("CatalogBuilderPage", () => {
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
    expect(screen.getByRole("button", { name: "Create catalog" })).toBeDisabled();
  });
});

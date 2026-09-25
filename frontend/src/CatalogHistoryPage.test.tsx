import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ApiError } from "./api";
import type { CatalogHistoryDetail, CatalogHistoryItem, CatalogHistoryPage as HistoryPage } from "./api";

const mocks = vi.hoisted(() => ({
  fetchCatalogHistory: vi.fn(), fetchCatalogHistoryOptions: vi.fn(), fetchCatalogHistoryDetail: vi.fn(),
}));
vi.mock("./api", async () => ({ ...await vi.importActual<typeof import("./api")>("./api"), ...mocks }));

import { CatalogHistoryDetailPage, CatalogHistoryPage } from "./CatalogHistoryPage";
import { App } from "./App";

const unavailable = {
  state: "unavailable" as const, key: null, version: null, display_name: null,
  title: null, subtitle: null, edition_label: null, heading: null, note: null,
  show_publisher_logo: false, hero_present: false, contacts: [], qr_target_type: null, qr_target_url: null,
};

function item(overrides: Partial<CatalogHistoryItem> = {}): CatalogHistoryItem {
  return {
    build_id: "11111111-1111-1111-1111-111111111111", status: "succeeded",
    created_at: "2026-09-23T12:00:00Z", completed_at: "2026-09-23T12:01:00Z",
    render_version: "catalog.render.v5",
    publisher: { key: "grabelan", display_name: "Grabelan Natural Market", primary_color: "#183D2F", accent_color: "#C89B3C", contact_text: null, social_handle: null, logo_present: false },
    product_count: 1, sku_count: 1, layout_key: "classic", layout_version: "1", layout_display_name: "Classic",
    theme_key: "premium", theme_version: "1", theme_display_name: "Premium", palette_source: "publisher", primary_color: "#183D2F", accent_color: "#C89B3C",
    cover: { ...unavailable, state: "enabled", key: "editorial", version: "1", display_name: "Editorial", title: "Autumn Catalog", edition_label: "2026" },
    closing: { ...unavailable, state: "enabled", key: "order", version: "1", display_name: "Order", heading: "How to order" },
    page_count: 6, artifact_available: true,
    artifact: { id: "pdf-1", created_at: "2026-09-23T12:01:00Z", page_count: 6, preview_url: "/api/catalog-builder/artifacts/pdf-1/pdf", download_url: "/api/catalog-builder/artifacts/pdf-1/pdf?download=true" },
    latest_render_status: "succeeded", error: null, historical_data_available: true,
    ...overrides,
  };
}

function page(items: CatalogHistoryItem[], overrides: Partial<HistoryPage> = {}): HistoryPage {
  return { items, page: 1, page_size: 20, total: items.length, all_total: items.length, ...overrides };
}

function detail(overrides: Partial<CatalogHistoryDetail> = {}): CatalogHistoryDetail {
  return {
    ...item(), snapshot_schema_version: "catalog-snapshot-v1", currency: "PYG", as_of: "2026-09-23T12:00:00Z",
    products: [{ category_name: "Proteínas", brand_name: "LANDERFIT", product_name: "Premium Whey", short_description: null,
      variants: [{ external_sku: "WHEY-1", flavor: "Vanilla", size_value: "2", size_unit: "LB", servings: null, price_amount: "350000", price_currency: "PYG" }] }],
    render_attempts: [{ attempt: 2, status: "succeeded", started_at: "2026-09-23T12:00:00Z", completed_at: "2026-09-23T12:01:00Z", page_count: 6, error: null, render_version: "catalog.render.v5" },
      { attempt: 1, status: "failed", started_at: "2026-09-23T11:00:00Z", completed_at: "2026-09-23T11:01:00Z", page_count: null, error: "Catalog rendering failed.", render_version: "catalog.render.v5" }],
    ...overrides,
  };
}

function renderHistory(path = "/catalogs") {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/catalogs" element={<CatalogHistoryPage />} />
    <Route path="/catalogs/:buildId" element={<CatalogHistoryDetailPage />} />
    <Route path="/catalog-builder" element={<div>Builder destination</div>} />
  </Routes></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchCatalogHistoryOptions.mockResolvedValue({ publishers: ["Grabelan Natural Market"], themes: ["premium"] });
  mocks.fetchCatalogHistory.mockResolvedValue(page([]));
  mocks.fetchCatalogHistoryDetail.mockResolvedValue(detail());
});

it("shows loading, empty library and create action", async () => {
  mocks.fetchCatalogHistory.mockReturnValue(new Promise(() => {}));
  const view = renderHistory();
  expect(screen.getByText("Loading catalogs…")).toBeInTheDocument();
  view.unmount();
  mocks.fetchCatalogHistory.mockResolvedValue(page([]));
  renderHistory();
  expect(await screen.findByText("No catalogs generated yet.")).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "Create catalog" })[0]).toHaveAttribute("href", "/catalog-builder");
});

it("reports errors and no matching results separately", async () => {
  mocks.fetchCatalogHistory.mockRejectedValueOnce(new Error("Network unavailable"));
  const view = renderHistory();
  expect(await screen.findByRole("alert")).toHaveTextContent("Network unavailable");
  view.unmount();
  mocks.fetchCatalogHistory.mockResolvedValue(page([], { all_total: 4 }));
  renderHistory();
  expect(await screen.findByText("No catalogs match these filters.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
});

it("lists statuses, controls, pagination and safe PDF actions", async () => {
  mocks.fetchCatalogHistory.mockResolvedValue(page([
    item(), item({ build_id: "22222222-2222-2222-2222-222222222222", status: "failed", artifact_available: false, artifact: null, page_count: null, error: "Catalog rendering failed." }),
    item({ build_id: "33333333-3333-3333-3333-333333333333", status: "running", artifact_available: false, artifact: null }),
  ], { total: 30, all_total: 30 }));
  renderHistory();
  expect((await screen.findAllByText("Grabelan Natural Market", { selector: "h2" })).length).toBe(3);
  expect(screen.getAllByText("PDF unavailable")).toHaveLength(2);
  expect(screen.getByRole("link", { name: "Preview PDF" })).toHaveAttribute("href", "/api/catalog-builder/artifacts/pdf-1/pdf");
  expect(screen.getByRole("link", { name: "Download PDF" })).toHaveAttribute("href", "/api/catalog-builder/artifacts/pdf-1/pdf?download=true");
  expect(screen.getAllByRole("link", { name: "Details" })[0]).toHaveAttribute("href", "/catalogs/11111111-1111-1111-1111-111111111111");
  expect(screen.getByText("Failed", { selector: "span" })).toBeInTheDocument();
  expect(screen.getByText("Running", { selector: "span" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Next" }));
  await waitFor(() => expect(mocks.fetchCatalogHistory).toHaveBeenLastCalledWith(2, expect.anything(), expect.anything()));
  await userEvent.selectOptions(screen.getByLabelText("Status"), "failed");
  await waitFor(() => expect(mocks.fetchCatalogHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({ status: "failed" }), expect.anything()));
  await userEvent.selectOptions(screen.getByLabelText("Publisher"), "Grabelan Natural Market");
  await userEvent.selectOptions(screen.getByLabelText("Theme"), "premium");
  await userEvent.selectOptions(screen.getByLabelText("Date"), "7d");
  await userEvent.type(screen.getByLabelText("Search catalogs"), "Autumn");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(mocks.fetchCatalogHistory).toHaveBeenLastCalledWith(1, expect.objectContaining({ search: "Autumn", status: "failed", publisher: "Grabelan Natural Market", theme: "premium", period: "7d" }), expect.anything()));
});

it("loads detail directly with frozen values and no mutation controls", async () => {
  renderHistory("/catalogs/11111111-1111-1111-1111-111111111111");
  expect(await screen.findByText("Premium Whey")).toBeInTheDocument();
  expect(screen.getByText(/Gs\. 350\.000/)).toBeInTheDocument();
  expect(screen.getByText("Proteínas · LANDERFIT")).toBeInTheDocument();
  expect(screen.getByText(/How to order/)).toBeInTheDocument();
  expect(screen.getByText(/Attempt 1 · failed/)).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "Preview PDF" }).length).toBeGreaterThan(0);
  for (const action of ["Edit", "Delete", "Rename", "Duplicate", "Rerender"]) {
    expect(screen.queryByRole("button", { name: action })).not.toBeInTheDocument();
  }
});

it("shows legacy, missing artifact, failed and unknown detail safely", async () => {
  mocks.fetchCatalogHistoryDetail.mockResolvedValue(detail({ ...item({ status: "failed", artifact_available: false, artifact: null, cover: unavailable, closing: unavailable, theme_display_name: null, error: "Catalog rendering failed." }), products: [], render_attempts: [] }));
  const view = renderHistory("/catalogs/11111111-1111-1111-1111-111111111111");
  expect(await screen.findByText("Artifact unavailable. Preview and download are disabled.")).toBeInTheDocument();
  expect(screen.getAllByText("Not available in this render version").length).toBeGreaterThan(0);
  expect(screen.queryByRole("link", { name: "Preview PDF" })).not.toBeInTheDocument();
  view.unmount();
  mocks.fetchCatalogHistoryDetail.mockRejectedValueOnce(new ApiError(404, "unknown"));
  renderHistory("/catalogs/99999999-9999-9999-9999-999999999999");
  expect(await screen.findByRole("alert")).toHaveTextContent("Catalog not found.");
});

it("makes Catalogs discoverable in the existing workspace navigation", async () => {
  render(<MemoryRouter initialEntries={["/catalogs"]}><App /></MemoryRouter>);
  expect(screen.getByRole("link", { name: "Catalogs" })).toHaveAttribute("href", "/catalogs");
  expect(await screen.findByText("No catalogs generated yet.")).toBeInTheDocument();
});

it.each([
  ["catalog.render.v2", unavailable, unavailable, null, 3, 0],
  ["catalog.render.v3", unavailable, unavailable, "Premium", 2, 0],
  ["catalog.render.v4", { ...unavailable, state: "disabled" as const }, unavailable, "Premium", 1, 1],
  ["catalog.render.v5", { ...unavailable, state: "disabled" as const }, { ...unavailable, state: "disabled" as const }, "Premium", 0, 2],
] as const)("keeps %s feature availability distinct from disabled choices", async (renderVersion, cover, closing, themeName, unavailableCount, disabledCount) => {
  mocks.fetchCatalogHistoryDetail.mockResolvedValue(detail({
    ...item({ render_version: renderVersion, cover, closing, theme_display_name: themeName }),
  }));
  renderHistory("/catalogs/11111111-1111-1111-1111-111111111111");
  const section = (await screen.findByRole("heading", { name: "Presentation" })).closest("section")!;
  expect(within(section).queryAllByText("Not available in this render version")).toHaveLength(unavailableCount);
  expect(within(section).queryAllByText("None")).toHaveLength(disabledCount);
});

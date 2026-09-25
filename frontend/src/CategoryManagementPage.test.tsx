import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ManagedCategory } from "./api";

const mocks = vi.hoisted(() => ({
  fetchManagedCategories: vi.fn(), fetchManagedCategory: vi.fn(),
  createManagedCategory: vi.fn(), updateManagedCategory: vi.fn(), setManagedCategoryActive: vi.fn(),
}));
vi.mock("./api", async () => ({ ...await vi.importActual<typeof import("./api")>("./api"), ...mocks }));

import { CategoryManagementPage } from "./CategoryManagementPage";

const makeCategory = (overrides: Partial<ManagedCategory> = {}): ManagedCategory => ({
  id: "11111111-1111-1111-1111-111111111111", name: "Proteínas", identity_key: "proteínas",
  is_active: true, sort_order: 1000, primary_product_count: 0,
  secondary_product_count: 0, total_product_count: 0, ...overrides,
});

let categories: ManagedCategory[];
function renderPage() { return render(<MemoryRouter><CategoryManagementPage /></MemoryRouter>); }

beforeEach(() => {
  vi.clearAllMocks();
  categories = [makeCategory()];
  mocks.fetchManagedCategories.mockImplementation(async (search: string, status: string, offset: number) => {
    const items = categories.filter((category) => (status === "all" || category.is_active === (status === "active")) && category.name.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
    return { items: items.slice(offset, offset + 100), total: items.length, all_total: categories.length, limit: 100, offset };
  });
  mocks.fetchManagedCategory.mockImplementation(async (id: string) => ({
    ...categories.find((category) => category.id === id)!, affected_products: [], affected_products_limit: 50,
  }));
  mocks.createManagedCategory.mockImplementation(async (name: string, sortOrder: number) => {
    const created = makeCategory({ id: "22222222-2222-2222-2222-222222222222", name, sort_order: sortOrder });
    categories.push(created); return created;
  });
  mocks.updateManagedCategory.mockImplementation(async (id: string, changes: Partial<ManagedCategory>) => {
    categories = categories.map((category) => category.id === id ? { ...category, ...changes } : category);
    return categories.find((category) => category.id === id);
  });
  mocks.setManagedCategoryActive.mockImplementation(async (id: string, active: boolean) => {
    categories = categories.map((category) => category.id === id ? { ...category, is_active: active } : category);
    return categories.find((category) => category.id === id);
  });
});

describe("Category Management", () => {
  it("defaults to active, searches, validates and creates a human Category once", async () => {
    const user = userEvent.setup(); renderPage();
    expect(await screen.findByText("Proteínas")).toBeInTheDocument();
    expect(mocks.fetchManagedCategories).toHaveBeenCalledWith("", "active", 0, expect.anything());
    await user.type(screen.getByLabelText("Search categories"), "missing");
    expect(await screen.findByText("No categories match this search.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search categories"));
    await user.click(screen.getByRole("button", { name: "+ New category" }));
    await user.click(within(screen.getByLabelText("Create category")).getByRole("button", { name: "Create" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a Category name");
    await user.type(screen.getByLabelText("Category name"), "Matcha");
    await user.click(within(screen.getByLabelText("Create category")).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mocks.createManagedCategory).toHaveBeenCalledTimes(1));
    expect(mocks.createManagedCategory).toHaveBeenCalledWith("Matcha", 1000);
    expect(await within(screen.getByLabelText("Category list")).findByText("Matcha")).toBeInTheDocument();
  });

  it("shows duplicate conflict and can open the existing Category", async () => {
    const user = userEvent.setup();
    mocks.createManagedCategory.mockRejectedValueOnce(new ApiError(409, "Category already exists.", { code: "duplicate_category", message: "Category already exists.", existing_category_id: categories[0].id }));
    renderPage(); await screen.findByText("Proteínas");
    await user.click(screen.getByRole("button", { name: "+ New category" }));
    await user.type(screen.getByLabelText("Category name"), "Proteínas");
    await user.click(within(screen.getByLabelText("Create category")).getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Category already exists.");
  });

  it("renames, shows usage, blocks used deactivation and reactivates unused Category", async () => {
    const user = userEvent.setup();
    categories = [makeCategory({ primary_product_count: 1, total_product_count: 1 })];
    renderPage(); await screen.findByText("Proteínas");
    await user.click(screen.getByRole("button", { name: "Manage" }));
    expect(await screen.findByText("1 Primary · 0 Secondary · 1 total Products")).toBeInTheDocument();
    expect(screen.getByText(/Reassign these Products/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deactivate" })).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Category name"));
    await user.type(screen.getByLabelText("Category name"), "Proteínas y suplementos");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(mocks.updateManagedCategory).toHaveBeenCalledWith(categories[0].id, { name: "Proteínas y suplementos", sort_order: 1000 }));
    categories = [makeCategory({ id: categories[0].id, name: "Proteínas y suplementos" })];
    await user.click(screen.getByRole("button", { name: "Manage" }));
    await screen.findByRole("button", { name: "Deactivate" });
    await user.click(screen.getByRole("button", { name: "Deactivate" }));
    expect(screen.getByText(/Deactivate “Proteínas y suplementos”/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm deactivation" }));
    await waitFor(() => expect(mocks.setManagedCategoryActive).toHaveBeenCalledWith(categories[0].id, false));
    await user.selectOptions(screen.getByLabelText("Category status filter"), "inactive");
    expect(await within(screen.getByLabelText("Category list")).findByText("Inactive")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reactivate" }));
    await waitFor(() => expect(mocks.setManagedCategoryActive).toHaveBeenCalledWith(categories[0].id, true));
  });

  it("shows rename and concurrent usage conflicts without closing the editor", async () => {
    const user = userEvent.setup(); renderPage();
    await screen.findByText("Proteínas");
    await user.click(screen.getByRole("button", { name: "Manage" }));
    await screen.findByRole("button", { name: "Deactivate" });
    mocks.updateManagedCategory.mockRejectedValueOnce(new ApiError(409, "Duplicate", {
      code: "duplicate_category", message: "Category already exists.", existing_category_id: "other-id",
    }));
    await user.clear(screen.getByLabelText("Category name"));
    await user.type(screen.getByLabelText("Category name"), "Existing");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Category already exists.");
    await user.click(screen.getByRole("button", { name: "Deactivate" }));
    mocks.setManagedCategoryActive.mockRejectedValueOnce(new ApiError(409, "In use", {
      code: "category_in_use", message: "Reassign first.", primary_product_count: 1, secondary_product_count: 2,
    }));
    await user.click(screen.getByRole("button", { name: "Confirm deactivation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("1 Products as Primary and 2 as Secondary");
    expect(screen.getByRole("button", { name: "Confirm deactivation" })).toBeInTheDocument();
  });

  it("disables a second create submission while the first is pending", async () => {
    const user = userEvent.setup();
    let finish!: (value: ManagedCategory) => void;
    mocks.createManagedCategory.mockImplementationOnce(() => new Promise<ManagedCategory>((resolve) => { finish = resolve; }));
    renderPage(); await screen.findByText("Proteínas");
    await user.click(screen.getByRole("button", { name: "+ New category" }));
    await user.type(screen.getByLabelText("Category name"), "Matcha");
    await user.click(within(screen.getByLabelText("Create category")).getByRole("button", { name: "Create" }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(mocks.createManagedCategory).toHaveBeenCalledTimes(1);
    const created = makeCategory({ id: "22222222-2222-2222-2222-222222222222", name: "Matcha" });
    categories.push(created);
    finish(created);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Saving…" })).not.toBeInTheDocument());
    expect(mocks.createManagedCategory).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

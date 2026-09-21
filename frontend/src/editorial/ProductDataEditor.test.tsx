import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ProductDataSummary } from "../api";
import { ProductDataEditor } from "./ProductDataEditor";

const data: ProductDataSummary = {
  product: {
    product_id: "product-1", product_name: "Whey", brand_name: "Landerfit",
    primary_category_id: "cat-1", primary_category_name: "Protein",
    readiness: { ready: true, blockers: [], warnings: [], presentation_warnings: [] },
    publishable_skus: [], hero: null, copy_state: "none", short_description: null,
  },
  brand_id: "brand-1", brands: [{ brand_id: "brand-1", name: "Landerfit" }, { brand_id: "brand-2", name: "Other" }],
  categories: [{ category_id: "cat-1", name: "Protein", assigned: true, is_primary: true }, { category_id: "cat-2", name: "Supplements", assigned: false, is_primary: false }],
  skus: [{ sku_id: "sku-1", flavor: "Vanilla", size_value: "2", size_unit: "LB", servings: 30, external_sku: null,
    active_price: { price_id: "price-1", amount: "360000.0000", currency: "PYG", valid_from: "2026-09-18T12:00:00Z", source: "human", approved: true }, price_history: [] }],
  identity_history: [],
};

describe("ProductDataEditor", () => {
  it("initializes each focused edit from backend data, cancels, and sends typed saves", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(true);
    render(<ProductDataEditor data={data} saving={false} onSave={onSave} />);
    await user.click(screen.getByRole("button", { name: "Edit Product" }));
    expect(screen.getByRole("textbox", { name: "Product name" })).toHaveValue("Whey");
    await user.clear(screen.getByRole("textbox", { name: "Product name" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Product name is required");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onSave).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Edit Product" }));
    await user.type(screen.getByRole("textbox", { name: "Product name" }), " Plus");
    await user.selectOptions(screen.getByRole("combobox", { name: "Product Brand" }), "brand-2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith({ kind: "identity", name: "Whey Plus", brandId: "brand-2" });

    await user.click(screen.getByRole("button", { name: "Edit Categories" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Primary Category" }), "cat-2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith({ kind: "categories", primaryId: "cat-2", secondaryIds: [] });

    await user.click(screen.getByRole("button", { name: "Edit variant" }));
    expect(screen.getByRole("spinbutton", { name: "size value" })).toHaveValue(2);
    await user.clear(screen.getByRole("textbox", { name: "flavor" }));
    await user.type(screen.getByRole("textbox", { name: "flavor" }), "Chocolate");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ kind: "sku", skuId: "sku-1", fields: expect.objectContaining({ flavor: "Chocolate" }) }));

    await user.click(screen.getByRole("button", { name: "Add variant" }));
    await user.type(screen.getByRole("textbox", { name: "flavor" }), "Strawberry");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ kind: "sku", skuId: null, fields: expect.objectContaining({ flavor: "Strawberry" }) }));

    await user.click(screen.getByRole("button", { name: "Change price" }));
    expect(screen.getByRole("spinbutton", { name: "Price amount" })).toHaveValue(360000);
    await user.clear(screen.getByRole("spinbutton", { name: "Price amount" }));
    await user.type(screen.getByRole("spinbutton", { name: "Price amount" }), "375000");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(onSave).toHaveBeenCalledWith({ kind: "price", skuId: "sku-1", amount: "375000", currency: "PYG" });
  }, 15000);

  it("disables duplicate submission while saving", async () => {
    const user = userEvent.setup();
    render(<ProductDataEditor data={data} saving={true} onSave={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Edit Product" }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  });
});

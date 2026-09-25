import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError, ManagedCategoryDetail, ManagedCategoryList, createManagedCategory,
  fetchManagedCategories, fetchManagedCategory, setManagedCategoryActive,
  updateManagedCategory,
} from "./api";

type Filter = "active" | "inactive" | "all";

export function CategoryManagementPage() {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("active");
  const [offset, setOffset] = useState(0);
  const [list, setList] = useState<ManagedCategoryList | null>(null);
  const [detail, setDetail] = useState<ManagedCategoryDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflictId, setConflictId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [order, setOrder] = useState("1000");
  const [confirmDeactivate, setConfirmDeactivate] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try { setList(await fetchManagedCategories(search, filter, offset, signal)); setError(null); }
    catch (reason) { if ((reason as Error).name !== "AbortError") { setList(null); setError((reason as Error).message); } }
    finally { if (!signal?.aborted) setLoading(false); }
  }, [search, filter, offset]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function select(id: string) {
    setSelectedId(id); setDetail(null); setConfirmDeactivate(false); setError(null);
    try {
      const fresh = await fetchManagedCategory(id);
      setDetail(fresh); setName(fresh.name); setOrder(String(fresh.sort_order));
    } catch (reason) { setError((reason as Error).message); }
  }

  function report(reason: unknown) {
    if (reason instanceof ApiError && typeof reason.detail === "object" && reason.detail !== null) {
      const detail = reason.detail as { message?: string; code?: string; existing_category_id?: string; primary_product_count?: number; secondary_product_count?: number };
      setConflictId(detail.code === "duplicate_category" ? detail.existing_category_id ?? null : null);
      setError(detail.code === "category_in_use"
        ? `Cannot deactivate this Category. Used by ${detail.primary_product_count} Products as Primary and ${detail.secondary_product_count} as Secondary. Reassign those Products first in the Product editor.`
        : detail.message ?? reason.message);
    } else setError(reason instanceof Error ? reason.message : "Category action failed.");
  }

  async function create() {
    if (busy) return;
    if (!name.trim() || !order.trim() || !Number.isInteger(Number(order)) || Number(order) < 0 || Number(order) > 2147483647) {
      setError("Enter a Category name and a nonnegative whole order number."); return;
    }
    setBusy(true); setError(null); setConflictId(null);
    try {
      const created = await createManagedCategory(name, Number(order));
      setCreating(false); setFilter("active"); setSearch(""); setOffset(0);
      setList(await fetchManagedCategories("", "active", 0)); await select(created.id);
    } catch (reason) { report(reason); }
    finally { setBusy(false); }
  }

  async function save() {
    if (!detail || busy) return;
    if (!name.trim() || !order.trim() || !Number.isInteger(Number(order)) || Number(order) < 0 || Number(order) > 2147483647) {
      setError("Enter a Category name and a nonnegative whole order number."); return;
    }
    setBusy(true); setError(null); setConflictId(null);
    try {
      await updateManagedCategory(detail.id, { name, sort_order: Number(order) });
      await load(); await select(detail.id);
    } catch (reason) { report(reason); }
    finally { setBusy(false); }
  }

  async function changeActive(active: boolean) {
    if (!detail || busy) return;
    setBusy(true); setError(null);
    try {
      await setManagedCategoryActive(detail.id, active);
      setConfirmDeactivate(false);
      await load(); await select(detail.id);
    } catch (reason) { report(reason); }
    finally { setBusy(false); }
  }

  return <main className="category-shell">
    <header className="intake-header"><div><p className="eyebrow">Workspace</p><h1>Categories</h1><p>Manage the canonical taxonomy used by Products and AI suggestions. Classification stays in the Product editor.</p></div>
      <button type="button" onClick={() => { setCreating(true); setDetail(null); setSelectedId(null); setName(""); setOrder("1000"); setError(null); }}>+ New category</button>
    </header>
    <p><Link to="/catalog-builder">Catalog Builder</Link> · <Link to="/product-intake">Product Intake</Link></p>
    <div className="category-toolbar">
      <label>Search categories<input value={search} onChange={(event) => { setSearch(event.target.value); setOffset(0); }} /></label>
      <label>Status<select aria-label="Category status filter" value={filter} onChange={(event) => { setFilter(event.target.value as Filter); setOffset(0); }}><option value="active">Active</option><option value="inactive">Inactive</option><option value="all">All</option></select></label>
    </div>
    {error && <div role="alert" className="intake-error">{error} {conflictId && <button type="button" onClick={() => { setCreating(false); void select(conflictId); }}>Open existing Category</button>}</div>}
    <div className="category-layout"><section className="intake-panel" aria-label="Category list">
      {loading ? <p>Loading categories…</p> : !list ? <p>Categories could not be loaded.</p> : list.items.length === 0 ? <div className="intake-empty"><p>{search ? "No categories match this search." : list.all_total === 0 ? "No categories yet." : filter === "active" ? "No active categories. Check Inactive or All." : filter === "inactive" ? "No inactive categories." : "No categories on this page."}</p><button type="button" onClick={() => { setCreating(true); setName(""); setOrder("1000"); }}>Create category</button></div> : <>
        <div className="category-table-wrap"><table className="category-table"><thead><tr><th>Category</th><th>Status</th><th>Primary</th><th>Secondary</th><th>Total</th><th>Order</th><th>Action</th></tr></thead><tbody>{list.items.map((category) => <tr key={category.id}><td>{category.name}</td><td>{category.is_active ? "Active" : "Inactive"}</td><td>{category.primary_product_count}</td><td>{category.secondary_product_count}</td><td>{category.total_product_count}</td><td>{category.sort_order}</td><td><button type="button" onClick={() => { setCreating(false); void select(category.id); }}>Manage</button></td></tr>)}</tbody></table></div>
        <div className="intake-actions"><span>{offset + 1}–{Math.min(offset + list.limit, list.total)} of {list.total}</span><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</button><button type="button" disabled={offset + list.limit >= list.total} onClick={() => setOffset(offset + 100)}>Next</button></div>
      </>}
    </section>
    {(creating || selectedId) && <section className="intake-panel" aria-label={creating ? "Create category" : "Edit category"}>
      <h2>{creating ? "New category" : detail?.name ?? "Loading category…"}</h2>
      {(creating || detail) && <>
        <label>Category name<input aria-label="Category name" maxLength={255} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} /></label>
        <label>Order<input aria-label="Category order" type="number" min="0" max="2147483647" step="1" value={order} disabled={busy} onChange={(event) => setOrder(event.target.value)} /></label>
        <p>Lower order numbers appear first in selectors and future catalog sections. Historical catalogs keep their frozen order.</p>
        <div className="intake-actions"><button type="button" disabled={busy} onClick={() => void (creating ? create() : save())}>{busy ? "Saving…" : creating ? "Create" : "Save changes"}</button><button type="button" className="secondary-button" disabled={busy} onClick={() => { setCreating(false); setSelectedId(null); setDetail(null); setError(null); }}>Close</button></div>
        {detail && !creating && <>
          <p>{detail.primary_product_count} Primary · {detail.secondary_product_count} Secondary · {detail.total_product_count} total Products</p>
          {detail.affected_products.length > 0 && <details><summary>Affected Products (up to {detail.affected_products_limit})</summary><ul>{detail.affected_products.map((product) => <li key={product.product_id}>{product.brand_name} · {product.product_name} ({product.role}) — <Link to={`/catalog-builder?product=${product.product_id}`}>Open Product</Link></li>)}</ul></details>}
          {detail.is_active ? detail.total_product_count ? <p>Reassign these Products in the Product editor before deactivating.</p> : confirmDeactivate ? <div className="category-confirm"><p>Deactivate “{detail.name}”? It will disappear from new assignments and AI suggestions.</p><button type="button" disabled={busy} onClick={() => void changeActive(false)}>Confirm deactivation</button><button type="button" className="secondary-button" onClick={() => setConfirmDeactivate(false)}>Cancel</button></div> : <button type="button" disabled={busy} onClick={() => setConfirmDeactivate(true)}>Deactivate</button> : <button type="button" disabled={busy} onClick={() => void changeActive(true)}>Reactivate</button>}
        </>}
      </>}
    </section>}
    </div>
  </main>;
}

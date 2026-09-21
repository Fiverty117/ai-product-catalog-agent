import { useCallback, useEffect, useMemo, useState } from "react";

import {
  BrandProfile,
  CatalogLayout,
  ProductSummary,
  fetchBrandProfiles,
  fetchLayouts,
  fetchProducts,
  resolveApiUrl,
} from "./api";
import { ProductEditorialDrawer } from "./editorial/ProductEditorialDrawer";

type ReadinessFilter = "all" | "ready" | "not_ready";

function formatPrice(amount: string, currency: string): string {
  if (currency !== "PYG") return `${currency} ${amount}`;
  const [integer, fraction = ""] = amount.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const meaningfulFraction = fraction.replace(/0+$/, "");
  return `Gs. ${grouped}${meaningfulFraction ? `,${meaningfulFraction}` : ""}`;
}

function ProductImage({ product }: { product: ProductSummary }) {
  const [failed, setFailed] = useState(false);
  const imageUrl = product.hero?.image_url;

  useEffect(() => setFailed(false), [imageUrl]);

  if (!imageUrl || failed) {
    return (
      <div className="product-image-placeholder" aria-label="No product image available">
        No image
      </div>
    );
  }
  return (
    <img
      className="product-image"
      src={resolveApiUrl(imageUrl)}
      alt={`${product.brand_name} ${product.product_name}`}
      onError={() => setFailed(true)}
    />
  );
}

function CopyBadge({ state }: { state: ProductSummary["copy_state"] }) {
  const label = state === "current" ? "Current" : state === "stale" ? "Stale" : "None";
  return <span className={`copy-badge copy-${state}`}>Copy: {label}</span>;
}

function ProductCard({
  product,
  selected,
  onToggle,
  onReviewCopy,
}: {
  product: ProductSummary;
  selected: boolean;
  onToggle: (product: ProductSummary, checked: boolean) => void;
  onReviewCopy: (productId: string) => void;
}) {
  const checkboxId = `select-${product.product_id}`;
  return (
    <article className={`product-card ${product.readiness.ready ? "" : "product-card-blocked"}`}>
      <div className="product-media">
        <ProductImage product={product} />
      </div>
      <div className="product-details">
        <div className="product-heading-row">
          <div>
            <p className="product-brand">{product.brand_name}</p>
            <h2>{product.product_name}</h2>
            <p className="product-category">
              {product.primary_category_name ?? "No primary category"}
            </p>
          </div>
          <label className={`selection-control ${product.readiness.ready ? "" : "is-disabled"}`} htmlFor={checkboxId}>
            <input
              id={checkboxId}
              type="checkbox"
              checked={selected}
              disabled={!product.readiness.ready}
              onChange={(event) => onToggle(product, event.target.checked)}
            />
            Select
          </label>
        </div>

        <div className="status-row">
          <span className={`readiness-badge ${product.readiness.ready ? "ready" : "not-ready"}`}>
            {product.readiness.ready ? "✓ Ready for catalog" : "Not ready"}
          </span>
          <CopyBadge state={product.copy_state} />
        </div>

        {!product.readiness.ready && (
          <div className="blocker-panel">
            <strong>Missing:</strong>
            <ul>
              {product.readiness.blockers.map((blocker) => (
                <li key={`${blocker.code}-${blocker.sku_id ?? "product"}`}>{blocker.message}</li>
              ))}
            </ul>
          </div>
        )}

        {product.readiness.presentation_warnings.length > 0 && (
          <p className="media-warning">The selected edited image is unavailable; the original is shown.</p>
        )}

        {product.short_description && (
          <p className="copy-preview">{product.short_description}</p>
        )}

        <div className="sku-list" aria-label={`${product.product_name} publishable variants`}>
          {product.publishable_skus.length === 0 ? (
            <p className="no-variants">No publishable variants</p>
          ) : (
            product.publishable_skus.map((sku) => (
              <div className="sku-row" key={sku.sku_id}>
                <span>{sku.variant_label}</span>
                <strong>{formatPrice(sku.active_price_amount, sku.currency)}</strong>
              </div>
            ))
          )}
        </div>
        <div className="product-card-actions">
          <button type="button" onClick={() => onReviewCopy(product.product_id)}>
            Review copy
          </button>
        </div>
      </div>
    </article>
  );
}

export function CatalogBuilderPage() {
  const [products, setProducts] = useState<ProductSummary[] | null>(null);
  const [profiles, setProfiles] = useState<BrandProfile[] | null>(null);
  const [layouts, setLayouts] = useState<CatalogLayout[] | null>(null);
  const [search, setSearch] = useState("");
  const [readinessFilter, setReadinessFilter] = useState<ReadinessFilter>("all");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectedProducts, setSelectedProducts] = useState<Record<string, ProductSummary>>({});
  const [brandProfileId, setBrandProfileId] = useState("");
  const [layoutKey, setLayoutKey] = useState<CatalogLayout["key"]>("classic");
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [editorialProductId, setEditorialProductId] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([fetchBrandProfiles(controller.signal), fetchLayouts(controller.signal)])
      .then(([nextProfiles, nextLayouts]) => {
        setProfiles(nextProfiles);
        setLayouts(nextLayouts);
        setBrandProfileId((current) => current || nextProfiles[0]?.id || "");
        if (!nextLayouts.some((layout) => layout.key === "classic") && nextLayouts[0]) {
          setLayoutKey(nextLayouts[0].key);
        }
      })
      .catch((caught: unknown) => {
        if ((caught as Error).name !== "AbortError") {
          setProfiles([]);
          setLayouts([]);
          setError("Catalog configuration could not be loaded.");
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setIsRefreshing(true);
    setError(null);
    fetchProducts(search, readinessFilter, controller.signal)
      .then((result) => {
        setProducts(result.products);
        setSelectedProducts((current) => {
          const next = { ...current };
          for (const product of result.products) {
            if (next[product.product_id]) next[product.product_id] = product;
          }
          return next;
        });
      })
      .catch((caught: unknown) => {
        if ((caught as Error).name !== "AbortError") {
          setProducts((current) => current ?? []);
          setError("Products could not be loaded. Check that the local API is running.");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsRefreshing(false);
      });
    return () => controller.abort();
  }, [search, readinessFilter]);

  const selectedProfile = profiles?.find((profile) => profile.id === brandProfileId);
  const selectedLayout = layouts?.find((layout) => layout.key === layoutKey);
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const allSelectedReady = selectedIds.every((id) => selectedProducts[id]?.readiness.ready);
  const readyCount = products?.filter((product) => product.readiness.ready).length ?? 0;

  const handleToggle = (product: ProductSummary, checked: boolean) => {
    if (!product.readiness.ready) return;
    setSelectedIds((current) => {
      const next = new Set(current);
      if (checked) next.add(product.product_id);
      else next.delete(product.product_id);
      return Array.from(next).sort();
    });
    setSelectedProducts((current) => {
      if (checked) return { ...current, [product.product_id]: product };
      const next = { ...current };
      delete next[product.product_id];
      return next;
    });
  };

  const handleProductUpdated = useCallback((product: ProductSummary) => {
    setProducts((current) =>
      current?.map((item) =>
        item.product_id === product.product_id ? product : item,
      ) ?? current,
    );
    setSelectedProducts((current) =>
      current[product.product_id]
        ? { ...current, [product.product_id]: product }
        : current,
    );
  }, []);

  const initialLoading = products === null || profiles === null || layouts === null;

  return (
    <main className="builder-shell">
      <header className="page-header">
        <p className="eyebrow">Catalog workspace</p>
        <h1>Catalog Builder</h1>
        <p>Choose ready products and presentation settings for a future catalog.</p>
      </header>

      <section className="configuration-bar" aria-labelledby="configuration-title">
        <div>
          <p className="section-kicker" id="configuration-title">Configuration</p>
          <p className="configuration-help">Publisher and layout are sourced from the catalog API.</p>
        </div>
        <label>
          Catalog brand
          <select
            value={brandProfileId}
            onChange={(event) => setBrandProfileId(event.target.value)}
            disabled={!profiles?.length}
          >
            {!profiles?.length && <option value="">No active brand profiles</option>}
            {profiles?.map((profile) => (
              <option value={profile.id} key={profile.id}>{profile.display_name}</option>
            ))}
          </select>
        </label>
        <label>
          Layout
          <select
            value={layoutKey}
            onChange={(event) => setLayoutKey(event.target.value as CatalogLayout["key"])}
            disabled={!layouts?.length}
          >
            {layouts?.map((layout) => (
              <option value={layout.key} key={`${layout.key}-${layout.version}`}>
                {layout.display_label}
              </option>
            ))}
          </select>
        </label>
      </section>

      {error && <div className="notice notice-error" role="alert">{error}</div>}
      {!initialLoading && profiles?.length === 0 && (
        <div className="notice" role="status">No active Catalog Brand Profile is available.</div>
      )}

      <div className="builder-grid">
        <section className="product-area" aria-labelledby="products-title">
          <div className="product-toolbar">
            <div>
              <p className="section-kicker">Products</p>
              <h2 id="products-title">Catalog-eligible inventory</h2>
            </div>
            <p className="selected-count">{selectedIds.length} selected</p>
          </div>
          <div className="filters">
            <label className="search-field">
              Search products or brands
              <input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search by product or brand"
              />
            </label>
            <label>
              Readiness
              <select
                value={readinessFilter}
                onChange={(event) => setReadinessFilter(event.target.value as ReadinessFilter)}
              >
                <option value="all">All</option>
                <option value="ready">Ready</option>
                <option value="not_ready">Not ready</option>
              </select>
            </label>
          </div>

          {initialLoading ? (
            <div className="state-panel" role="status">Loading Catalog Builder…</div>
          ) : products.length === 0 ? (
            <div className="state-panel">
              <strong>No products found.</strong>
              <span>Try another search or readiness filter.</span>
            </div>
          ) : (
            <>
              {readyCount === 0 && readinessFilter !== "not_ready" && (
                <div className="notice" role="status">No products in this result are ready for a catalog.</div>
              )}
              <div className={`product-list ${isRefreshing ? "is-refreshing" : ""}`}>
                {products.map((product) => (
                  <ProductCard
                    key={product.product_id}
                    product={product}
                    selected={selectedSet.has(product.product_id)}
                    onToggle={handleToggle}
                    onReviewCopy={setEditorialProductId}
                  />
                ))}
              </div>
            </>
          )}
        </section>

        <aside className="composition-panel" aria-labelledby="composition-title">
          <p className="section-kicker">Composition</p>
          <h2 id="composition-title">Catalog summary</h2>
          {selectedIds.length === 0 && (
            <p className="empty-selection">No products selected yet.</p>
          )}
          <dl>
            <div><dt>Products selected</dt><dd>{selectedIds.length}</dd></div>
            <div><dt>Brand</dt><dd>{selectedProfile?.display_name ?? "Not selected"}</dd></div>
            <div><dt>Layout</dt><dd>{selectedLayout?.display_label ?? "Not available"}</dd></div>
            <div><dt>Columns</dt><dd>{selectedLayout?.products_per_row ?? "—"}</dd></div>
            <div><dt>Page</dt><dd>{selectedLayout ? `${selectedLayout.page_size} · ${selectedLayout.orientation}` : "—"}</dd></div>
            <div><dt>All selected Products ready</dt><dd>{selectedIds.length > 0 && allSelectedReady ? "Yes" : "—"}</dd></div>
          </dl>
          <button type="button" disabled>Create catalog</button>
          <p className="future-note">Catalog creation and publishing arrive in a later block.</p>
        </aside>
      </div>
      {editorialProductId && (
        <ProductEditorialDrawer
          productId={editorialProductId}
          onClose={() => setEditorialProductId(null)}
          onProductUpdated={handleProductUpdated}
        />
      )}
    </main>
  );
}

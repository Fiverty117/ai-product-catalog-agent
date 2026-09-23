import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  BrandProfile,
  CatalogBuild,
  CatalogBuildInput,
  CatalogBuildReadinessConflict,
  CatalogLayout,
  ProductSummary,
  ApiError,
  createCatalogBuild,
  fetchBrandProfiles,
  fetchCatalogBuild,
  fetchLayouts,
  fetchProducts,
  retryCatalogBuild,
  resolveApiUrl,
} from "./api";
import { ProductEditorialDrawer } from "./editorial/ProductEditorialDrawer";

type ReadinessFilter = "all" | "ready" | "not_ready";
const BUILD_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const BUILD_POLL_TIMEOUT_MS = 10 * 60 * 1000;

function rememberBuildId(buildId: string): void {
  const url = new URL(window.location.href);
  url.searchParams.set("build", buildId);
  window.history.replaceState(window.history.state, "", url);
}

function formatPrice(amount: string, currency: string): string {
  if (currency !== "PYG") return `${currency} ${amount}`;
  const [integer, fraction = ""] = amount.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const meaningfulFraction = fraction.replace(/0+$/, "");
  return `Gs. ${grouped}${meaningfulFraction ? `,${meaningfulFraction}` : ""}`;
}

function ProductImage({ product }: { product: ProductSummary }) {
  const [failed, setFailed] = useState(false);
  const hero = product.hero;
  const imageUrl = hero?.image_url;
  const presentationKey = hero ? `${hero.source_photo_id}:${hero.effective_derived_image_id ?? "original"}` : "none";

  useEffect(() => setFailed(false), [imageUrl, presentationKey]);

  if (!hero || failed) {
    return (
      <div className="product-image-placeholder" aria-label="No product image available">
        No image
      </div>
    );
  }
  return (
    <img
      key={presentationKey}
      className="product-image"
      src={resolveApiUrl(hero.image_url)}
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
              disabled={!product.readiness.ready && !selected}
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
            Edit Product
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
  const [refreshToken, setRefreshToken] = useState(0);
  const [editorialProductId, setEditorialProductId] = useState<string | null>(null);
  const [build, setBuild] = useState<CatalogBuild | null>(null);
  const [buildError, setBuildError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [pollExpired, setPollExpired] = useState(false);
  const submittingRef = useRef(false);
  const pendingCreateRef = useRef<CatalogBuildInput | null>(null);
  const pollStartedAtRef = useRef(Date.now());

  useEffect(() => {
    const buildId = new URLSearchParams(window.location.search).get("build");
    if (!buildId || !BUILD_ID_PATTERN.test(buildId)) return;
    const controller = new AbortController();
    fetchCatalogBuild(buildId, controller.signal)
      .then((restored) => {
        pollStartedAtRef.current = Date.now();
        setBuild(restored);
      })
      .catch((caught: unknown) => {
        if ((caught as Error).name !== "AbortError") {
          setBuildError("The saved Catalog build could not be restored.");
        }
      });
    return () => controller.abort();
  }, []);

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
  }, [search, readinessFilter, refreshToken]);

  useEffect(() => {
    if (!build || pollExpired || (build.status !== "queued" && build.status !== "running")) return;
    const controller = new AbortController();
    let requestInFlight = false;
    const timer = window.setInterval(() => {
      if (requestInFlight) return;
      if (Date.now() - pollStartedAtRef.current >= BUILD_POLL_TIMEOUT_MS) {
        setPollExpired(true);
        return;
      }
      requestInFlight = true;
      fetchCatalogBuild(build.id, controller.signal)
        .then((next) => {
          if (controller.signal.aborted) return;
          setBuild(next);
          setBuildError(null);
        })
        .catch((caught: unknown) => {
          if ((caught as Error).name !== "AbortError") {
            setBuildError("Catalog status could not be refreshed.");
          }
        })
        .finally(() => { requestInFlight = false; });
    }, 2000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [build, pollExpired]);

  const selectedProfile = profiles?.find((profile) => profile.id === brandProfileId);
  const selectedLayout = layouts?.find((layout) => layout.key === layoutKey);
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const allSelectedReady = selectedIds.every((id) => selectedProducts[id]?.readiness.ready);
  const readyCount = products?.filter((product) => product.readiness.ready).length ?? 0;

  const handleToggle = (product: ProductSummary, checked: boolean) => {
    if (!product.readiness.ready && checked) return;
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

  const applyReadinessConflict = (conflict: CatalogBuildReadinessConflict) => {
    setSelectedProducts((current) => {
      const next = { ...current };
      for (const failure of conflict.products) {
        const product = next[failure.product_id];
        if (product) {
          next[failure.product_id] = {
            ...product,
            readiness: {
              ...product.readiness,
              ready: false,
              blockers: failure.blockers,
            },
          };
        }
      }
      return next;
    });
    const productNames = conflict.products.map(
      (failure) => selectedProducts[failure.product_id]?.product_name ?? failure.product_id,
    );
    setBuildError(
      `${conflict.message} Needs attention: ${productNames.join(", ")}.`,
    );
    setRefreshToken((value) => value + 1);
  };

  const handleCreate = async () => {
    if (
      submittingRef.current || selectedIds.length === 0 ||
      !brandProfileId || !selectedLayout
    ) return;
    submittingRef.current = true;
    setIsCreating(true);
    setBuildError(null);
    const choices = {
      product_ids: [...selectedIds],
      catalog_brand_profile_id: brandProfileId,
      layout_key: selectedLayout.key,
      layout_version: selectedLayout.version,
      currency: "PYG",
    };
    const pending = pendingCreateRef.current;
    const input: CatalogBuildInput = pending && JSON.stringify({
      product_ids: pending.product_ids,
      catalog_brand_profile_id: pending.catalog_brand_profile_id,
      layout_key: pending.layout_key,
      layout_version: pending.layout_version,
      currency: pending.currency,
    }) === JSON.stringify(choices)
      ? pending
      : { ...choices, idempotency_key: crypto.randomUUID() };
    pendingCreateRef.current = input;
    try {
      const next = await createCatalogBuild(input);
      pollStartedAtRef.current = Date.now();
      setPollExpired(false);
      setBuild(next);
      rememberBuildId(next.id);
      pendingCreateRef.current = null;
    } catch (caught: unknown) {
      const apiError = caught as ApiError;
      const detail = apiError.detail as Partial<CatalogBuildReadinessConflict> | undefined;
      if (
        apiError.status === 409 && detail?.code === "catalog_readiness_changed" &&
        Array.isArray(detail.products) && typeof detail.message === "string"
      ) {
        pendingCreateRef.current = null;
        applyReadinessConflict(detail as CatalogBuildReadinessConflict);
      } else {
        if (apiError.status === 409) pendingCreateRef.current = null;
        setBuildError(apiError.message || "Catalog creation could not be started.");
      }
    } finally {
      submittingRef.current = false;
      setIsCreating(false);
    }
  };

  const handleRetry = async () => {
    if (!build || isRetrying) return;
    setIsRetrying(true);
    setBuildError(null);
    try {
      pollStartedAtRef.current = Date.now();
      setPollExpired(false);
      setBuild(await retryCatalogBuild(build.id));
    } catch (caught: unknown) {
      setBuildError((caught as Error).message || "Catalog render could not be retried.");
    } finally {
      setIsRetrying(false);
    }
  };

  const canCreate = selectedIds.length > 0 && Boolean(brandProfileId && selectedLayout);

  return (
    <main className="builder-shell">
      <header className="page-header">
        <p className="eyebrow">Catalog workspace</p>
        <h1>Catalog Builder</h1>
        <p>Choose ready products and presentation settings, then generate a PDF catalog.</p>
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
          <button
            type="button"
            className="create-catalog-action"
            disabled={!canCreate || isCreating}
            onClick={(event) => {
              if (event.detail < 2) void handleCreate();
            }}
          >
            {isCreating ? "Creating snapshot…" : "Create catalog"}
          </button>
          {buildError && <p className="build-error" role="alert">{buildError}</p>}
          {build && (
            <section className={`build-result build-${build.status}`} aria-live="polite">
              <p className="section-kicker">Current build</p>
              <h3>
                {build.status === "succeeded" && "Catalog ready"}
                {build.status === "queued" && "Catalog queued"}
                {build.status === "running" && "Generating catalog…"}
                {build.status === "failed" && "Catalog generation failed"}
              </h3>
              <p>
                {build.product_count} {build.product_count === 1 ? "Product" : "Products"}
                {" · "}{build.catalog_brand_display_name}
                {" · "}{build.layout_display_label}
                {build.artifact ? ` · ${build.artifact.page_count} ${build.artifact.page_count === 1 ? "page" : "pages"}` : ""}
              </p>
              {(build.status === "queued" || build.status === "running") && (
                <p className="build-progress" role="status">
                  {pollExpired
                    ? "Status updates paused. Reload this page to check the same catalog build."
                    : build.status === "queued"
                      ? "Waiting for the local render worker…"
                      : "Rendering the frozen catalog…"}
                </p>
              )}
              {build.status === "failed" && (
                <>
                  <p className="build-failure-message">{build.error ?? "Catalog rendering failed."}</p>
                  {build.can_retry && (
                    <button type="button" onClick={handleRetry} disabled={isRetrying}>
                      {isRetrying ? "Retrying…" : "Retry render"}
                    </button>
                  )}
                </>
              )}
              {build.status === "succeeded" && build.artifact && (
                <div className="artifact-actions">
                  <a
                    href={resolveApiUrl(build.artifact.preview_url)}
                    target="_blank"
                    rel="noreferrer"
                  >Preview PDF</a>
                  <a href={resolveApiUrl(build.artifact.download_url)}>Download PDF</a>
                </div>
              )}
            </section>
          )}
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

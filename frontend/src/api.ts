export type ReadinessIssue = {
  code: string;
  severity: "blocker" | "warning";
  scope: "product" | "sku" | "category";
  message: string;
  product_id: string | null;
  sku_id: string | null;
  category_id: string | null;
  photo_id: string | null;
};

export type ProductSummary = {
  product_id: string;
  product_name: string;
  brand_name: string;
  primary_category_id: string | null;
  primary_category_name: string | null;
  readiness: {
    ready: boolean;
    blockers: ReadinessIssue[];
    warnings: ReadinessIssue[];
    presentation_warnings: Array<
      "preferred_derived_asset_missing" | "preferred_derived_selection_invalid"
    >;
  };
  publishable_skus: Array<{
    sku_id: string;
    variant_label: string;
    flavor: string | null;
    size_value: string | null;
    size_unit: string | null;
    active_price_amount: string;
    currency: string;
  }>;
  hero: {
    source_photo_id: string;
    effective_derived_image_id: string | null;
    presentation_type: "original" | "derived";
    image_url: string;
  } | null;
  copy_state: "current" | "stale" | "none";
  short_description: string | null;
};

export type ProductList = {
  currency: string;
  as_of: string;
  products: ProductSummary[];
};

export type BrandProfile = {
  id: string;
  key: string;
  display_name: string;
  logo_url: string | null;
  primary_color: string;
  accent_color: string;
};

export type CatalogLayout = {
  key: "classic" | "dense" | "compact";
  version: string;
  display_label: string;
  products_per_row: number;
  page_size: "A4";
  orientation: "portrait";
};

const apiBase = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export function resolveApiUrl(path: string): string {
  return `${apiBase}${path}`;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(resolveApiUrl(path), {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function fetchProducts(
  search: string,
  readiness: "all" | "ready" | "not_ready",
  signal?: AbortSignal,
): Promise<ProductList> {
  const params = new URLSearchParams({ currency: "PYG", readiness });
  if (search.trim()) params.set("search", search.trim());
  return getJson<ProductList>(`/api/catalog-builder/products?${params}`, signal);
}

export function fetchBrandProfiles(signal?: AbortSignal): Promise<BrandProfile[]> {
  return getJson<BrandProfile[]>("/api/catalog-builder/brand-profiles", signal);
}

export function fetchLayouts(signal?: AbortSignal): Promise<CatalogLayout[]> {
  return getJson<CatalogLayout[]>("/api/catalog-builder/layouts", signal);
}

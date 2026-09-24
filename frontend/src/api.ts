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

export type CatalogBuild = {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  catalog_snapshot_id: string;
  product_count: number;
  currency: string;
  catalog_brand_profile_id: string;
  catalog_brand_key: string;
  catalog_brand_display_name: string;
  layout_key: CatalogLayout["key"];
  layout_version: string;
  layout_display_label: string;
  created_at: string;
  error: string | null;
  can_retry: boolean;
  artifact: {
    id: string;
    created_at: string;
    page_count: number;
    preview_url: string;
    download_url: string;
  } | null;
};

export type CatalogBuildReadinessConflict = {
  code: "catalog_readiness_changed";
  message: string;
  products: Array<{
    product_id: string;
    blockers: ReadinessIssue[];
  }>;
};

export type ProductCopyReviewState = "unreviewed" | "approved" | "corrected" | "rejected";

export type ProductCopyEditorialReview = {
  decision: "approved" | "corrected" | "rejected";
  corrected_short_description: string | null;
  created_at: string;
};

export type ProductCopyEditorialRun = {
  run_id: string;
  job_id: string | null;
  status: "running" | "succeeded" | "failed";
  review_state: ProductCopyReviewState;
  generated_text: string | null;
  sanitized_error: string | null;
  source_state: "current" | "stale";
  review: ProductCopyEditorialReview | null;
  provider: string;
  model: string;
  created_at: string;
  completed_at: string | null;
};

export type ProductCopyGeneration = {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attempts: number;
  max_attempts: number;
  can_retry: boolean;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
};

export type ProductCopyEditorialSummary = {
  product: ProductSummary;
  effective_copy: {
    state: "current" | "stale" | "none";
    short_description: string | null;
  };
  generations: ProductCopyGeneration[];
  runs: ProductCopyEditorialRun[];
  manual_revisions: {
    revision_id: string;
    short_description: string;
    source_state: "current" | "stale";
    created_at: string;
  }[];
  has_active_generation: boolean;
};

export type ProductCopyGenerationResponse = {
  generation: ProductCopyGeneration;
  editorial: ProductCopyEditorialSummary;
};

export type ProductCopyReviewResponse = {
  review: ProductCopyEditorialReview;
  editorial: ProductCopyEditorialSummary;
};

export type ProductDataSummary = {
  product: ProductSummary;
  brand_id: string;
  brands: { brand_id: string; name: string }[];
  categories: { category_id: string; name: string; is_primary: boolean; assigned: boolean }[];
  skus: {
    sku_id: string; external_sku: string | null; flavor: string | null;
    size_value: string | null; size_unit: string | null; servings: number | null;
    active_price: ProductDataPrice | null; price_history: ProductDataPrice[];
  }[];
  identity_history: { old_name: string; new_name: string; old_brand_id: string; new_brand_id: string; created_at: string }[];
};

export type ProductDataPrice = {
  price_id: string; amount: string; currency: string; valid_from: string;
  source: string; approved: boolean;
};

export type ProductImageEditorialSummary = {
  product_id: string;
  source_photo_id: string | null;
  source_owner: "product" | "sku" | null;
  source_sku_id: string | null;
  original_preview_url: string | null;
  effective: {
    presentation: "original" | "derived";
    derived_image_id: string | null;
    preview_url: string;
    warnings: Array<"preferred_derived_asset_missing" | "preferred_derived_selection_invalid">;
  } | null;
  derived_images: {
    derived_image_id: string;
    review_state: "unreviewed" | "approved" | "rejected";
    selectable: boolean;
    asset_available: boolean;
    selected: boolean;
    preview_url: string | null;
    created_at: string;
  }[];
  generations: ProductImageGeneration[];
  can_generate: boolean;
  product: ProductSummary;
};

export type ProductImageGeneration = {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  attempts: number;
  max_attempts: number;
  can_retry: boolean;
  created_at: string;
};

export type ProductImageGenerationResponse = { generation: ProductImageGeneration; editorial: ProductImageEditorialSummary };

export function fetchProductImageEditorial(productId: string, signal?: AbortSignal): Promise<ProductImageEditorialSummary> {
  return getJson(`/api/products/${productId}/images/editorial`, signal);
}

export function generateProductImage(productId: string, photoId: string, idempotencyKey: string): Promise<ProductImageGenerationResponse> {
  return requestJson(`/api/products/${productId}/images/photos/${photoId}/enhancements`, {
    method: "POST", headers: { "Idempotency-Key": idempotencyKey },
  });
}

export function retryProductImage(productId: string, jobId: string): Promise<ProductImageGenerationResponse> {
  return requestJson(`/api/products/${productId}/images/enhancements/${jobId}/retry`, { method: "POST" });
}

export function reviewProductDerivedImage(productId: string, derivedImageId: string, decision: "approved" | "rejected"): Promise<ProductImageEditorialSummary> {
  return requestJson(`/api/products/${productId}/images/derived/${derivedImageId}/review`, { method: "POST", body: JSON.stringify({ decision }) });
}

export function selectProductImagePresentation(productId: string, photoId: string, derivedImageId: string | null): Promise<ProductImageEditorialSummary> {
  return requestJson(`/api/products/${productId}/images/photos/${photoId}/presentation`, {
    method: "PUT",
    body: JSON.stringify(derivedImageId ? { presentation: "derived", derived_image_id: derivedImageId } : { presentation: "original" }),
  });
}

export type SKUDataInput = {
  external_sku: string | null; flavor: string | null; size_value: string | null;
  size_unit: string | null; servings: number | null;
};

export function fetchProductData(productId: string, signal?: AbortSignal): Promise<ProductDataSummary> {
  return getJson(`/api/products/${productId}/data`, signal);
}

function saveProductData(productId: string, path: string, method: "PUT" | "POST", body: object): Promise<ProductDataSummary> {
  return requestJson(`/api/products/${productId}/data/${path}`, { method, body: JSON.stringify(body) });
}

export const saveProductIdentity = (productId: string, name: string, brandId: string) =>
  saveProductData(productId, "identity", "PUT", { name, brand_id: brandId });
export const saveProductCategories = (productId: string, primaryId: string | null, secondaryIds: string[]) =>
  saveProductData(productId, "categories", "PUT", { primary_category_id: primaryId, secondary_category_ids: secondaryIds });
export const saveProductSKU = (productId: string, skuId: string, fields: SKUDataInput) =>
  saveProductData(productId, `skus/${skuId}`, "PUT", fields);
export const addProductSKU = (productId: string, fields: SKUDataInput) =>
  saveProductData(productId, "skus", "POST", fields);
export const changeProductPrice = (productId: string, skuId: string, amount: string, currency: string) =>
  saveProductData(productId, `skus/${skuId}/prices`, "POST", { amount, currency });

const apiBase = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

export function resolveApiUrl(path: string): string {
  return `${apiBase}${path}`;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly detail: unknown = message,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function requestJson<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(resolveApiUrl(path), {
    headers: { Accept: "application/json" },
    ...options,
    ...(options.body
      ? { headers: { Accept: "application/json", "Content-Type": "application/json", ...options.headers } }
      : {}),
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    let rawDetail: unknown = detail;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      rawDetail = payload.detail ?? detail;
      if (typeof payload.detail === "string") detail = payload.detail;
      else if (Array.isArray(payload.detail) && typeof payload.detail[0]?.msg === "string") detail = payload.detail[0].msg;
      else if (
        payload.detail && typeof payload.detail === "object" &&
        "message" in payload.detail && typeof payload.detail.message === "string"
      ) detail = payload.detail.message;
    } catch {
      // Keep the concise status fallback when a response is not JSON.
    }
    throw new ApiError(response.status, detail, rawDetail);
  }
  return response.json() as Promise<T>;
}

function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return requestJson<T>(path, { signal });
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

export type CatalogBuildInput = {
  product_ids: string[];
  catalog_brand_profile_id: string;
  layout_key: CatalogLayout["key"];
  layout_version: string;
  currency: string;
  idempotency_key: string;
};

export function createCatalogBuild(input: CatalogBuildInput): Promise<CatalogBuild> {
  return requestJson<CatalogBuild>("/api/catalog-builder/builds", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function fetchCatalogBuild(
  buildId: string,
  signal?: AbortSignal,
): Promise<CatalogBuild> {
  return getJson<CatalogBuild>(`/api/catalog-builder/builds/${buildId}`, signal);
}

export function retryCatalogBuild(buildId: string): Promise<CatalogBuild> {
  return requestJson<CatalogBuild>(
    `/api/catalog-builder/builds/${buildId}/retry`,
    { method: "POST" },
  );
}

export function fetchProductCopyEditorial(
  productId: string,
  signal?: AbortSignal,
): Promise<ProductCopyEditorialSummary> {
  return getJson<ProductCopyEditorialSummary>(
    `/api/products/${productId}/product-copy`,
    signal,
  );
}

export function generateProductCopy(
  productId: string,
  idempotencyKey: string = crypto.randomUUID(),
): Promise<ProductCopyGenerationResponse> {
  return requestJson<ProductCopyGenerationResponse>(
    `/api/products/${productId}/product-copy/generations`,
    { method: "POST", headers: { "Idempotency-Key": idempotencyKey } },
  );
}

export function retryProductCopyGeneration(
  productId: string,
  jobId: string,
): Promise<ProductCopyGenerationResponse> {
  return requestJson<ProductCopyGenerationResponse>(
    `/api/products/${productId}/product-copy/generations/${jobId}/retry`,
    { method: "POST" },
  );
}

export function reviewProductCopy(
  productId: string,
  runId: string,
  decision: "approved" | "corrected" | "rejected",
  correctedShortDescription?: string,
): Promise<ProductCopyReviewResponse> {
  return requestJson<ProductCopyReviewResponse>(
    `/api/products/${productId}/product-copy/runs/${runId}/review`,
    {
      method: "POST",
      body: JSON.stringify({
        decision,
        ...(decision === "corrected"
          ? { corrected_short_description: correctedShortDescription }
          : {}),
      }),
    },
  );
}

export function saveManualProductCopyRevision(
  productId: string,
  shortDescription: string,
): Promise<{ editorial: ProductCopyEditorialSummary }> {
  return requestJson(`/api/products/${productId}/product-copy/manual-revisions`, {
    method: "POST",
    body: JSON.stringify({ short_description: shortDescription }),
  });
}

export type IntakeSKU = {
  flavor: string | null;
  size_value: string | null;
  size_unit: string | null;
  servings: number | null;
  external_sku: string | null;
};

export type IntakeDraft = {
  schema_version: 1;
  brand_name: string | null;
  product_name: string | null;
  primary_category_name: string | null;
  secondary_category_names: string[];
  skus: IntakeSKU[];
  notes: string | null;
};

export type IntakeItem = {
  id: string;
  status: "draft" | "queued" | "running" | "review_required" | "failed" | "promoted";
  created_at: string;
  updated_at: string;
  draft: IntakeDraft;
  human_edited: boolean;
  photos: { id: string; position: number; is_primary: boolean; original_filename: string; mime_type: string; image_url: string }[];
  extraction: {
    job_id: string | null; job_status: string | null; attempts: number | null;
    max_attempts: number | null; run_id: string | null; run_status: string | null;
    error: string | null; newer_result_available: boolean;
    observation: Record<"brand_name" | "product_name" | "flavor" | "size_value" | "size_unit" | "servings", { value: string | number | null; state: "extracted" | "not_legible" | "not_present" }> | null;
  };
  promotion: { product_id: string; promoted_at: string } | null;
};

export type IntakePromotionResult = {
  status: "promoted"; readiness_currency: "PYG"; intake_id: string; product_id: string;
  brand_id: string; brand_name: string; brand_reused: boolean;
  promoted_at: string; sku_ids: string[]; product: ProductSummary;
};

export type IntakePromotionContext = {
  categories: { id: string; name: string }[];
  result: IntakePromotionResult | null;
};

export type IntakePromotionRequest = {
  idempotency_key: string;
  primary_category_id: string | null;
  secondary_category_ids: string[];
  sku_prices: { intake_sku_index: number; amount: string; currency: string }[];
};

export const fetchIntakePromotion = (id: string, signal?: AbortSignal): Promise<IntakePromotionContext> =>
  getJson(`/api/product-intake/items/${id}/promotion`, signal);
export const createIntakePromotion = (id: string, request: IntakePromotionRequest): Promise<IntakePromotionResult> =>
  requestJson(`/api/product-intake/items/${id}/promotion`, { method: "POST", body: JSON.stringify(request) });

export const fetchIntakeItems = (signal?: AbortSignal): Promise<{ items: IntakeItem[] }> =>
  getJson("/api/product-intake/items", signal);
export const fetchIntakeItem = (id: string, signal?: AbortSignal): Promise<IntakeItem> =>
  getJson(`/api/product-intake/items/${id}`, signal);

export async function createIntakeItem(files: File[]): Promise<IntakeItem> {
  const form = new FormData();
  files.forEach((file) => form.append("images", file));
  const response = await fetch(resolveApiUrl("/api/product-intake/items"), {
    method: "POST", headers: { Accept: "application/json" }, body: form,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new ApiError(response.status, payload?.detail ?? `Upload failed (${response.status})`);
  }
  return response.json() as Promise<IntakeItem>;
}

export const saveIntakeDraft = (id: string, draft: IntakeDraft): Promise<IntakeItem> =>
  requestJson(`/api/product-intake/items/${id}/draft`, { method: "PUT", body: JSON.stringify(draft) });
export const runIntakeExtraction = (id: string, key: string): Promise<IntakeItem> =>
  requestJson(`/api/product-intake/items/${id}/extractions`, { method: "POST", headers: { "Idempotency-Key": key } });
export const setIntakePrimaryPhoto = (id: string, photoId: string): Promise<IntakeItem> =>
  requestJson(`/api/product-intake/items/${id}/primary-photo`, { method: "PUT", body: JSON.stringify({ photo_id: photoId }) });

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  BrandProfile,
  CatalogBuild,
  CatalogBuildInput,
  CatalogBuildReadinessConflict,
  CatalogLayout,
  CatalogTheme,
  CatalogCoverLayout,
  CatalogCoverAsset,
  CatalogCoverInput,
  CatalogClosingLayout,
  CatalogClosingInput,
  ProductSummary,
  ApiError,
  createCatalogBuild,
  fetchBrandProfiles,
  fetchCatalogBuild,
  fetchLayouts,
  fetchThemes,
  fetchCoverLayouts,
  fetchClosingLayouts,
  fetchProducts,
  retryCatalogBuild,
  resolveApiUrl,
  uploadCatalogCoverAsset,
} from "./api";
import { ProductEditorialDrawer } from "./editorial/ProductEditorialDrawer";

type ReadinessFilter = "all" | "ready" | "not_ready";
const BUILD_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const BUILD_POLL_TIMEOUT_MS = 10 * 60 * 1000;
const VALID_COLOR = /^#[0-9A-Fa-f]{6}$/;
const COVER_TEXT_HAS_MARKUP = /[<>]/;
const COVER_MAX_FILE_BYTES = 10 * 1024 * 1024;
const CLOSING_CONTACT_KEYS = ["publisher_contact", "publisher_social", "whatsapp", "phone", "instagram", "website", "address"] as const;
type ClosingContactKey = (typeof CLOSING_CONTACT_KEYS)[number];
type ClosingContactState = { enabled: boolean; useCustom: boolean; value: string };
const CLOSING_CONTACT_LABELS: Record<ClosingContactKey, string> = {
  publisher_contact: "Publisher contact", publisher_social: "Publisher social",
  whatsapp: "WhatsApp", phone: "Phone", instagram: "Instagram", website: "Website", address: "Address",
};
function emptyClosingContacts(): Record<ClosingContactKey, ClosingContactState> {
  return Object.fromEntries(CLOSING_CONTACT_KEYS.map((key) => [key, { enabled: false, useCustom: false, value: "" }])) as Record<ClosingContactKey, ClosingContactState>;
}
function validHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && Boolean(url.hostname) && !url.username && !url.password && !/\s|\\|[<>]/.test(value);
  } catch { return false; }
}

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
  const [themes, setThemes] = useState<CatalogTheme[] | null>(null);
  const [coverLayouts, setCoverLayouts] = useState<CatalogCoverLayout[] | null>(null);
  const [closingLayouts, setClosingLayouts] = useState<CatalogClosingLayout[] | null>(null);
  const [search, setSearch] = useState("");
  const [readinessFilter, setReadinessFilter] = useState<ReadinessFilter>("all");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectedProducts, setSelectedProducts] = useState<Record<string, ProductSummary>>({});
  const [brandProfileId, setBrandProfileId] = useState("");
  const [layoutKey, setLayoutKey] = useState<CatalogLayout["key"]>("classic");
  const [themeKey, setThemeKey] = useState<CatalogTheme["key"]>("minimal");
  const [primaryOverride, setPrimaryOverride] = useState<string | null>(null);
  const [accentOverride, setAccentOverride] = useState<string | null>(null);
  const [coverEnabled, setCoverEnabled] = useState(false);
  const [coverKey, setCoverKey] = useState<CatalogCoverLayout["key"]>("minimal");
  const [coverTitle, setCoverTitle] = useState("Catálogo");
  const [coverSubtitle, setCoverSubtitle] = useState("");
  const [coverEdition, setCoverEdition] = useState("");
  const [showPublisherLogo, setShowPublisherLogo] = useState(true);
  const [coverHero, setCoverHero] = useState<CatalogCoverAsset | null>(null);
  const [coverUploadError, setCoverUploadError] = useState<string | null>(null);
  const [isUploadingCover, setIsUploadingCover] = useState(false);
  const [closingEnabled, setClosingEnabled] = useState(false);
  const [closingKey, setClosingKey] = useState<CatalogClosingLayout["key"]>("contact");
  const [closingHeading, setClosingHeading] = useState("Hacé tu pedido");
  const [closingNote, setClosingNote] = useState("");
  const [closingShowLogo, setClosingShowLogo] = useState(true);
  const [closingContacts, setClosingContacts] = useState<Record<ClosingContactKey, ClosingContactState>>(emptyClosingContacts);
  const [closingQrEnabled, setClosingQrEnabled] = useState(false);
  const [closingQrTarget, setClosingQrTarget] = useState<"whatsapp" | "website" | "custom_url">("whatsapp");
  const [closingQrCustomUrl, setClosingQrCustomUrl] = useState("");
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
    const productId = new URLSearchParams(window.location.search).get("product");
    if (productId && BUILD_ID_PATTERN.test(productId)) setEditorialProductId(productId);
  }, []);

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
    Promise.all([fetchBrandProfiles(controller.signal), fetchLayouts(controller.signal), fetchThemes(controller.signal), fetchCoverLayouts(controller.signal), fetchClosingLayouts(controller.signal)])
      .then(([nextProfiles, nextLayouts, nextThemes, nextCoverLayouts, nextClosingLayouts]) => {
        setProfiles(nextProfiles);
        setLayouts(nextLayouts);
        setThemes(nextThemes);
        setCoverLayouts(nextCoverLayouts);
        setClosingLayouts(nextClosingLayouts);
        setBrandProfileId((current) => current || nextProfiles[0]?.id || "");
        if (!nextLayouts.some((layout) => layout.key === "classic") && nextLayouts[0]) {
          setLayoutKey(nextLayouts[0].key);
        }
        if (!nextThemes.some((theme) => theme.key === "minimal") && nextThemes[0]) setThemeKey(nextThemes[0].key);
      })
      .catch((caught: unknown) => {
        if ((caught as Error).name !== "AbortError") {
          setProfiles([]);
          setLayouts([]);
          setThemes([]);
          setCoverLayouts([]);
          setClosingLayouts([]);
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
  const selectedTheme = themes?.find((theme) => theme.key === themeKey);
  const selectedCoverLayout = coverLayouts?.find((layout) => layout.key === coverKey);
  const selectedClosingLayout = closingLayouts?.find((layout) => layout.key === closingKey);
  const customPalette = primaryOverride !== null || accentOverride !== null;
  const primaryColor = primaryOverride ?? selectedProfile?.primary_color ?? "#000000";
  const accentColor = accentOverride ?? selectedProfile?.accent_color ?? "#000000";
  const paletteValid = VALID_COLOR.test(primaryColor) && VALID_COLOR.test(accentColor);
  const normalizedCoverTitle = coverTitle.trim().replace(/\s+/g, " ");
  const normalizedCoverSubtitle = coverSubtitle.trim().replace(/\s+/g, " ");
  const normalizedCoverEdition = coverEdition.trim().replace(/\s+/g, " ");
  const coverTextValid = !coverEnabled || (
    normalizedCoverTitle.length > 0 && normalizedCoverTitle.length <= 80 &&
    normalizedCoverSubtitle.length <= 180 && normalizedCoverEdition.length <= 60 &&
    ![normalizedCoverTitle, normalizedCoverSubtitle, normalizedCoverEdition].some((value) => COVER_TEXT_HAS_MARKUP.test(value))
  );
  const coverValid = !coverEnabled || (Boolean(selectedCoverLayout) && coverTextValid && (!selectedCoverLayout?.requires_hero || Boolean(coverHero)));
  const normalizedClosingHeading = closingHeading.trim().replace(/\s+/g, " ");
  const normalizedClosingNote = closingNote.trim().replace(/\s+/g, " ");
  const effectiveClosingValue = (key: ClosingContactKey): string => {
    const state = closingContacts[key];
    if (key === "publisher_contact" && !state.useCustom) return selectedProfile?.contact_text ?? "";
    if (key === "publisher_social" && !state.useCustom) return selectedProfile?.social_handle ?? "";
    return state.value.trim();
  };
  const enabledClosingKeys = CLOSING_CONTACT_KEYS.filter((key) => closingContacts[key].enabled);
  const closingContactsValid = enabledClosingKeys.every((key) => {
    const value = effectiveClosingValue(key);
    if (!value || /[<>\x00-\x1f]/.test(value)) return false;
    if (key === "website") return validHttpUrl(value);
    if (key === "instagram") return /^@?[A-Za-z0-9._]{1,30}$/.test(value);
    if (key === "whatsapp" || key === "phone") return /^\+?[0-9][0-9(). -]*$/.test(value) && value.replace(/\D/g, "").length >= 4;
    return value.length <= (key === "address" ? 240 : key === "publisher_contact" ? 500 : 255);
  });
  const qrTargetPreview = closingQrTarget === "custom_url" ? closingQrCustomUrl.trim() : effectiveClosingValue(closingQrTarget);
  const closingQrValid = !closingQrEnabled || (closingQrTarget === "custom_url"
    ? validHttpUrl(qrTargetPreview)
    : closingContacts[closingQrTarget].enabled && (closingQrTarget === "website" ? validHttpUrl(qrTargetPreview) : /^\+[0-9][0-9(). -]*$/.test(qrTargetPreview)));
  const closingValid = !closingEnabled || (Boolean(selectedClosingLayout) && normalizedClosingHeading.length <= 80 && normalizedClosingNote.length <= 300 &&
    !COVER_TEXT_HAS_MARKUP.test(normalizedClosingHeading + normalizedClosingNote) && closingContactsValid && closingQrValid &&
    normalizedClosingHeading.length + normalizedClosingNote.length + enabledClosingKeys.reduce((count, key) => count + effectiveClosingValue(key).length, 0) <= 900 &&
    Boolean(normalizedClosingHeading || normalizedClosingNote || enabledClosingKeys.length || closingQrEnabled) &&
    (!selectedClosingLayout?.requires_contact || enabledClosingKeys.length > 0));
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

  const initialLoading = products === null || profiles === null || layouts === null || themes === null || coverLayouts === null || closingLayouts === null;

  const resetClosing = () => {
    setClosingEnabled(false);
    setClosingKey("contact");
    setClosingHeading("Hacé tu pedido");
    setClosingNote("");
    setClosingShowLogo(true);
    setClosingContacts(emptyClosingContacts());
    setClosingQrEnabled(false);
    setClosingQrTarget("whatsapp");
    setClosingQrCustomUrl("");
  };

  const resetCover = () => {
    setCoverEnabled(false);
    setCoverKey("minimal");
    setCoverTitle("Catálogo");
    setCoverSubtitle("");
    setCoverEdition("");
    setShowPublisherLogo(true);
    setCoverHero(null);
    setCoverUploadError(null);
  };

  const handleHeroUpload = async (file: File | undefined) => {
    if (!file) return;
    setCoverUploadError(null);
    if (file.size > COVER_MAX_FILE_BYTES) {
      setCoverUploadError("Cover image must be 10 MiB or smaller.");
      return;
    }
    setIsUploadingCover(true);
    try {
      setCoverHero(await uploadCatalogCoverAsset(file));
    } catch (caught: unknown) {
      setCoverUploadError((caught as Error).message || "Cover image upload failed.");
    } finally {
      setIsUploadingCover(false);
    }
  };

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
      !brandProfileId || !selectedLayout || !selectedTheme || !paletteValid || !coverValid || !closingValid || isUploadingCover
    ) return;
    submittingRef.current = true;
    setIsCreating(true);
    setBuildError(null);
    const coverChoice: CatalogCoverInput = coverEnabled ? {
      enabled: true, cover_key: selectedCoverLayout!.key, cover_version: selectedCoverLayout!.version,
      title: normalizedCoverTitle,
      ...(normalizedCoverSubtitle ? { subtitle: normalizedCoverSubtitle } : {}),
      ...(normalizedCoverEdition ? { edition_label: normalizedCoverEdition } : {}),
      show_publisher_logo: showPublisherLogo && Boolean(selectedProfile?.logo_url),
      ...(coverHero ? { hero_asset_id: coverHero.asset_id } : {}),
    } : { enabled: false };
    const closingChoice: CatalogClosingInput = closingEnabled ? {
      enabled: true, closing_key: selectedClosingLayout!.key, closing_version: selectedClosingLayout!.version,
      ...(normalizedClosingHeading ? { heading: normalizedClosingHeading } : {}),
      ...(normalizedClosingNote ? { note: normalizedClosingNote } : {}),
      show_publisher_logo: closingShowLogo && Boolean(selectedProfile?.logo_url),
      ...Object.fromEntries(CLOSING_CONTACT_KEYS.map((key) => [key, closingContacts[key].enabled
        ? { enabled: true, ...((key !== "publisher_contact" && key !== "publisher_social") || closingContacts[key].useCustom
          ? { override: closingContacts[key].value.trim() } : {}) }
        : { enabled: false }])),
      qr: closingQrEnabled ? {
        enabled: true, target_type: closingQrTarget,
        ...(closingQrTarget === "custom_url" ? { custom_url: closingQrCustomUrl.trim() } : {}),
      } : { enabled: false },
    } as CatalogClosingInput : { enabled: false };
    const choices = {
      product_ids: [...selectedIds],
      catalog_brand_profile_id: brandProfileId,
      layout_key: selectedLayout.key,
      layout_version: selectedLayout.version,
      theme_key: selectedTheme.key,
      theme_version: selectedTheme.version,
      ...(primaryOverride !== null ? { primary_color_override: primaryOverride.toUpperCase() } : {}),
      ...(accentOverride !== null ? { accent_color_override: accentOverride.toUpperCase() } : {}),
      cover: coverChoice,
      closing: closingChoice,
      currency: "PYG",
    };
    const pending = pendingCreateRef.current;
    const input: CatalogBuildInput = pending && JSON.stringify({
      product_ids: pending.product_ids,
      catalog_brand_profile_id: pending.catalog_brand_profile_id,
      layout_key: pending.layout_key,
      layout_version: pending.layout_version,
      theme_key: pending.theme_key,
      theme_version: pending.theme_version,
      primary_color_override: pending.primary_color_override,
      accent_color_override: pending.accent_color_override,
      cover: pending.cover,
      closing: pending.closing,
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

  const canCreate = selectedIds.length > 0 && Boolean(brandProfileId && selectedLayout && selectedTheme && paletteValid && coverValid && closingValid && !isUploadingCover);

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
          <p className="configuration-help">Publisher, layout and theme are sourced from the catalog API.</p>
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
        <div className="theme-control" role="group" aria-label="Catalog theme">
          <span>Theme</span>
          <div className="theme-option-grid">
            {themes?.map((theme) => <label className={`theme-option theme-preview-${theme.key}`} key={`${theme.key}-${theme.version}`}>
              <input type="radio" name="catalog-theme" value={theme.key} checked={themeKey === theme.key} onChange={() => setThemeKey(theme.key)} />
              <span className="theme-swatch" aria-hidden="true" />
              <strong>{theme.display_name}</strong><small>{theme.description}</small>
            </label>)}
          </div>
        </div>
        <div className="palette-control" role="group" aria-label="Catalog palette">
          <strong>Palette</strong>
          <p>{customPalette ? "Custom palette" : `Using ${selectedProfile?.display_name ?? "publisher"} colors`}</p>
          <div className="palette-fields">
            <label>Primary
              <input aria-label="Primary color picker" type="color" value={VALID_COLOR.test(primaryColor) ? primaryColor : selectedProfile?.primary_color ?? "#000000"} onChange={(event) => setPrimaryOverride(event.target.value.toUpperCase())} />
              <input aria-label="Primary hex" type="text" value={primaryColor} maxLength={7} onChange={(event) => setPrimaryOverride(event.target.value)} />
            </label>
            <label>Accent
              <input aria-label="Accent color picker" type="color" value={VALID_COLOR.test(accentColor) ? accentColor : selectedProfile?.accent_color ?? "#000000"} onChange={(event) => setAccentOverride(event.target.value.toUpperCase())} />
              <input aria-label="Accent hex" type="text" value={accentColor} maxLength={7} onChange={(event) => setAccentOverride(event.target.value)} />
            </label>
          </div>
          {!paletteValid && <p className="field-error" role="alert">Use #RRGGBB for both palette colors.</p>}
          {customPalette && <button type="button" onClick={() => { setPrimaryOverride(null); setAccentOverride(null); }}>Reset to publisher colors</button>}
        </div>
        <div className="cover-control" role="group" aria-label="Catalog cover">
          <div className="cover-control-heading">
            <div><strong>Cover</strong><p>Optional front page for this catalog edition.</p></div>
            <label className="cover-enable"><input type="checkbox" checked={coverEnabled} onChange={(event) => setCoverEnabled(event.target.checked)} /> Include cover page</label>
          </div>
          {coverEnabled && <div className="cover-options">
            <div role="group" aria-label="Cover style">
              <span>Cover style</span>
              <div className="cover-layout-grid">
                {coverLayouts?.map((layout) => <label className={`cover-layout-option cover-layout-${layout.key}`} key={`${layout.key}-${layout.version}`}>
                  <input type="radio" name="catalog-cover" value={layout.key} checked={coverKey === layout.key} onChange={() => setCoverKey(layout.key)} />
                  <strong>{layout.display_name}</strong><small>{layout.description}</small>
                </label>)}
              </div>
            </div>
            <div className="cover-text-fields">
              <label>Catalog title<input type="text" value={coverTitle} maxLength={80} onChange={(event) => setCoverTitle(event.target.value)} /></label>
              <label>Subtitle (optional)<input type="text" value={coverSubtitle} maxLength={180} onChange={(event) => setCoverSubtitle(event.target.value)} /></label>
              <label>Edition (optional)<input type="text" value={coverEdition} maxLength={60} onChange={(event) => setCoverEdition(event.target.value)} /></label>
            </div>
            <label className="cover-logo-control"><input type="checkbox" checked={showPublisherLogo && Boolean(selectedProfile?.logo_url)} disabled={!selectedProfile?.logo_url} onChange={(event) => setShowPublisherLogo(event.target.checked)} /> Show publisher logo{!selectedProfile?.logo_url ? " (no logo available)" : ""}</label>
            <div className="cover-upload">
              <label>Hero image (optional for Minimal/Editorial)
                <input type="file" accept="image/png,image/jpeg,image/webp" disabled={isUploadingCover} onChange={(event) => { void handleHeroUpload(event.target.files?.[0]); event.target.value = ""; }} />
              </label>
              {isUploadingCover && <p role="status">Uploading cover image…</p>}
              {coverUploadError && <p className="field-error" role="alert">{coverUploadError}</p>}
              {coverHero && <div className="cover-uploaded">
                <img src={resolveApiUrl(coverHero.preview_url)} alt="Uploaded cover Hero preview" />
                <span>{coverHero.width} × {coverHero.height}</span>
                <button type="button" onClick={() => setCoverHero(null)}>Remove Hero</button>
              </div>}
            </div>
            {!coverTextValid && <p className="field-error" role="alert">Cover title is required (max 80 characters); subtitle/edition must be plain text within their limits.</p>}
            {selectedCoverLayout?.requires_hero && !coverHero && <p className="field-error" role="alert">Hero cover requires an uploaded image.</p>}
            <button type="button" className="cover-reset" onClick={resetCover}>Reset cover</button>
          </div>}
        </div>
        <div className="closing-control" role="group" aria-label="Catalog closing page">
          <div className="cover-control-heading">
            <div><strong>Closing &amp; ordering</strong><p>Optional final page. Contact details and QR are frozen for this build.</p></div>
            <label className="cover-enable"><input type="checkbox" checked={closingEnabled} onChange={(event) => {
              setClosingEnabled(event.target.checked);
              if (event.target.checked) setClosingContacts((current) => ({
                ...current,
                publisher_contact: { ...current.publisher_contact, enabled: current.publisher_contact.enabled || Boolean(selectedProfile?.contact_text) },
                publisher_social: { ...current.publisher_social, enabled: current.publisher_social.enabled || Boolean(selectedProfile?.social_handle) },
              }));
            }} /> Include closing page</label>
          </div>
          {closingEnabled && <div className="closing-options">
            <div role="group" aria-label="Closing style">
              <span>Closing style</span>
              <div className="cover-layout-grid">
                {closingLayouts?.map((layout) => <label className="cover-layout-option" key={`${layout.key}-${layout.version}`}>
                  <input type="radio" name="catalog-closing" value={layout.key} checked={closingKey === layout.key} onChange={() => setClosingKey(layout.key)} />
                  <strong>{layout.display_name}</strong><small>{layout.description}</small>
                </label>)}
              </div>
            </div>
            <div className="closing-text-fields">
              <label>Heading (optional)<input type="text" maxLength={80} value={closingHeading} onChange={(event) => setClosingHeading(event.target.value)} /></label>
              <label>Short note (optional)<textarea maxLength={300} rows={3} value={closingNote} onChange={(event) => setClosingNote(event.target.value)} /></label>
            </div>
            <label className="cover-logo-control"><input type="checkbox" checked={closingShowLogo && Boolean(selectedProfile?.logo_url)} disabled={!selectedProfile?.logo_url} onChange={(event) => setClosingShowLogo(event.target.checked)} /> Show publisher logo{!selectedProfile?.logo_url ? " (no logo available)" : ""}</label>
            <fieldset className="closing-contacts">
              <legend>Contact methods</legend>
              <p>Publisher contact and social values come from the selected frozen profile unless you override them here. Other methods are for this build only.</p>
              {CLOSING_CONTACT_KEYS.map((key) => {
                const state = closingContacts[key];
                const fromProfile = key === "publisher_contact" ? selectedProfile?.contact_text : key === "publisher_social" ? selectedProfile?.social_handle : null;
                return <div className="closing-contact-row" key={key}>
                  <label className="cover-enable"><input type="checkbox" checked={state.enabled} onChange={(event) => setClosingContacts((current) => ({ ...current, [key]: { ...current[key], enabled: event.target.checked } }))} /> {CLOSING_CONTACT_LABELS[key]}</label>
                  {state.enabled && <div className="closing-contact-value">
                    {fromProfile !== null && fromProfile !== undefined && !state.useCustom && <span className="closing-profile-value">From publisher: {fromProfile || "No value"}</span>}
                    {(key === "publisher_contact" || key === "publisher_social") && <label className="cover-enable"><input type="checkbox" checked={state.useCustom} onChange={(event) => setClosingContacts((current) => ({ ...current, [key]: { ...current[key], useCustom: event.target.checked } }))} /> Override for this build</label>}
                    {((key !== "publisher_contact" && key !== "publisher_social") || state.useCustom) && <input
                      aria-label={`${CLOSING_CONTACT_LABELS[key]} value`} type="text" maxLength={key === "publisher_contact" ? 500 : key === "address" ? 240 : 255}
                      placeholder={key === "website" ? "https://example.com" : key === "whatsapp" ? "+595..." : undefined}
                      value={state.value} onChange={(event) => setClosingContacts((current) => ({ ...current, [key]: { ...current[key], value: event.target.value } }))} />}
                  </div>}
                </div>;
              })}
            </fieldset>
            <fieldset className="closing-qr">
              <legend>QR code</legend>
              <label className="cover-enable"><input type="checkbox" checked={closingQrEnabled} onChange={(event) => setClosingQrEnabled(event.target.checked)} /> Include QR code</label>
              {closingQrEnabled && <>
                <label>QR destination<select value={closingQrTarget} onChange={(event) => setClosingQrTarget(event.target.value as typeof closingQrTarget)}>
                  <option value="whatsapp">Visible WhatsApp number</option><option value="website">Visible website</option><option value="custom_url">Custom URL</option>
                </select></label>
                {closingQrTarget === "custom_url" && <label>Custom QR URL<input type="url" value={closingQrCustomUrl} placeholder="https://example.com/order" onChange={(event) => setClosingQrCustomUrl(event.target.value)} /></label>}
                <small>QR destination: {qrTargetPreview || "Choose a valid destination"}</small>
              </>}
            </fieldset>
            {!closingValid && <p className="field-error" role="alert">Complete the closing page with valid plain text, contact methods, and any QR destination. Order style needs a contact method.</p>}
            <button type="button" className="cover-reset" onClick={resetClosing}>Reset closing</button>
          </div>}
        </div>
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
            <div><dt>Theme</dt><dd>{selectedTheme?.display_name ?? "Not available"}</dd></div>
            <div><dt>Palette</dt><dd>{customPalette ? "Custom" : "Publisher colors"}</dd></div>
            <div><dt>Cover</dt><dd>{coverEnabled ? selectedCoverLayout?.display_name ?? "Not available" : "None"}</dd></div>
            {coverEnabled && <div><dt>Title</dt><dd>{normalizedCoverTitle || "—"}</dd></div>}
            {coverEnabled && normalizedCoverEdition && <div><dt>Edition</dt><dd>{normalizedCoverEdition}</dd></div>}
            {coverEnabled && <div><dt>Hero</dt><dd>{coverHero ? "Custom image" : "None"}</dd></div>}
            <div><dt>Closing</dt><dd>{closingEnabled ? selectedClosingLayout?.display_name ?? "Not available" : "None"}</dd></div>
            {closingEnabled && <div><dt>Contacts</dt><dd>{enabledClosingKeys.length ? enabledClosingKeys.map((key) => CLOSING_CONTACT_LABELS[key]).join(", ") : "None"}</dd></div>}
            {closingEnabled && <div><dt>QR</dt><dd>{closingQrEnabled ? closingQrTarget.replace("_", " ") : "None"}</dd></div>}
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
                {" · "}{build.theme_display_label ?? "Legacy"}
                {" · "}{build.palette_source === "custom" ? "Custom palette" : build.palette_source === "publisher" ? "Publisher colors" : "Legacy palette"}
                {" · Cover: "}{build.cover_enabled ? build.cover_display_label ?? "Cover" : "None"}
                {build.cover_title ? ` · ${build.cover_title}` : ""}
                {build.cover_edition_label ? ` · ${build.cover_edition_label}` : ""}
                {build.cover_hero_present ? " · Custom Hero" : ""}
                {" · Closing: "}{build.closing_enabled ? build.closing_display_label ?? "Closing" : "None"}
                {build.closing_enabled && build.closing_contacts?.length ? ` · ${build.closing_contacts.map((contact) => CLOSING_CONTACT_LABELS[contact.kind as ClosingContactKey] ?? contact.kind).join(", ")}` : ""}
                {build.closing_qr_enabled ? ` · QR: ${build.closing_qr_target_type?.replace("_", " ")}` : ""}
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

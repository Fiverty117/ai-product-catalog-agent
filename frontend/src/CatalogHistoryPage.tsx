import { FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  ApiError, CatalogHistoryDetail, CatalogHistoryFilters, CatalogHistoryItem,
  CatalogHistoryPage as HistoryPageData, HistoryFeature,
  fetchCatalogHistory, fetchCatalogHistoryDetail, fetchCatalogHistoryOptions,
  resolveApiUrl,
} from "./api";

const defaultFilters: CatalogHistoryFilters = {
  search: "", status: "all", publisher: "", theme: "", period: "all",
};

function dateTime(value: string | null): string {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
}

function statusLabel(status: CatalogHistoryItem["status"]): string {
  return { queued: "Queued", running: "Running", succeeded: "Ready", failed: "Failed" }[status];
}

function featureLabel(feature: HistoryFeature): string {
  if (feature.state === "unavailable") return "Not available in this render version";
  if (feature.state === "invalid") return "Historical data unavailable";
  if (feature.state === "disabled") return "None";
  return feature.display_name ?? `${feature.key} v${feature.version}`;
}

function PdfActions({ item }: { item: CatalogHistoryItem }) {
  if (!item.artifact_available || !item.artifact) return <span className="history-unavailable">PDF unavailable</span>;
  return <span className="history-pdf-actions">
    <a href={resolveApiUrl(item.artifact.preview_url)} target="_blank" rel="noreferrer">Preview PDF</a>
    <a href={resolveApiUrl(item.artifact.download_url)}>Download PDF</a>
  </span>;
}

export function CatalogHistoryPage() {
  const [filters, setFilters] = useState<CatalogHistoryFilters>(defaultFilters);
  const [searchDraft, setSearchDraft] = useState("");
  const [page, setPage] = useState(1);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<HistoryPageData | null>(null);
  const [options, setOptions] = useState<{ publishers: string[]; themes: string[] }>({ publishers: [], themes: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchCatalogHistoryOptions(controller.signal).then(setOptions).catch(() => {
      if (!controller.signal.aborted) setOptions({ publishers: [], themes: [] });
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchCatalogHistory(page, filters, controller.signal)
      .then((result) => { setData(result); setLoading(false); })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : "Could not load catalogs.");
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [page, filters, refresh]);

  function updateFilter<K extends keyof CatalogHistoryFilters>(key: K, value: CatalogHistoryFilters[K]) {
    setPage(1);
    setFilters((previous) => ({ ...previous, [key]: value }));
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    updateFilter("search", searchDraft.trim());
  }

  function clearFilters() {
    setSearchDraft("");
    setPage(1);
    setFilters(defaultFilters);
  }

  return <main className="history-shell">
    <header className="history-header">
      <div><p className="eyebrow">Catalog library</p><h1>Catalogs</h1><p>Browse generated catalogs and their frozen configuration.</p></div>
      <div className="history-header-actions"><Link to="/catalog-builder">Create catalog</Link><button type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh</button></div>
    </header>
    <div className="history-filters">
      <form onSubmit={submitSearch}><label htmlFor="history-search">Search catalogs</label><div className="history-search"><input id="history-search" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} placeholder="Publisher, cover title or edition" /><button type="submit">Search</button></div></form>
      <label>Status<select value={filters.status} onChange={(event) => updateFilter("status", event.target.value as CatalogHistoryFilters["status"])}><option value="all">All</option><option value="ready">Ready</option><option value="active">Queued / Running</option><option value="failed">Failed</option></select></label>
      <label>Publisher<select value={filters.publisher} onChange={(event) => updateFilter("publisher", event.target.value)}><option value="">All</option>{options.publishers.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Theme<select value={filters.theme} onChange={(event) => updateFilter("theme", event.target.value)}><option value="">All</option>{options.themes.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Date<select value={filters.period} onChange={(event) => updateFilter("period", event.target.value as CatalogHistoryFilters["period"])}><option value="all">All time</option><option value="today">Today (UTC)</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option></select></label>
    </div>
    {error && <div className="history-message history-error" role="alert">{error} <button type="button" onClick={() => setRefresh((value) => value + 1)}>Try again</button></div>}
    {loading && <div className="history-message" role="status">Loading catalogs…</div>}
    {!loading && !error && data && data.all_total === 0 && <div className="history-message"><h2>No catalogs generated yet.</h2><p>Create a catalog to see its history here.</p><Link to="/catalog-builder">Create catalog</Link></div>}
    {!loading && !error && data && data.all_total > 0 && data.total === 0 && <div className="history-message"><h2>No catalogs match these filters.</h2><button type="button" onClick={clearFilters}>Clear filters</button></div>}
    {!loading && !error && data && data.items.length > 0 && <>
      <p className="history-count">{data.total} {data.total === 1 ? "catalog" : "catalogs"} · newest first</p>
      <div className="history-list">
        {data.items.map((item) => <article className="history-card" key={item.build_id}>
          <div className="history-card-main"><time dateTime={item.created_at}>{dateTime(item.created_at)}</time><h2>{item.publisher?.display_name ?? "Historical data unavailable"}</h2><p>{item.cover.title || item.cover.edition_label || "Catalog"}</p></div>
          <div className="history-card-facts"><span>{item.product_count ?? "—"} products</span><span>{item.layout_display_name ?? "—"} · {item.theme_display_name ?? (item.publisher ? "Legacy" : "Unavailable")}</span><span>Cover: {featureLabel(item.cover)}</span><span>Closing: {featureLabel(item.closing)}</span><span>{item.page_count ?? "—"} pages</span></div>
          <div className="history-card-actions"><span className={`history-status history-${item.status}`}>{statusLabel(item.status)}</span><PdfActions item={item} /><Link to={`/catalogs/${item.build_id}`}>Details</Link></div>
        </article>)}
      </div>
      <nav className="history-pagination" aria-label="Catalog pages"><button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button><span>Page {data.page} of {Math.ceil(data.total / data.page_size)}</span><button type="button" disabled={page * data.page_size >= data.total} onClick={() => setPage(page + 1)}>Next</button></nav>
    </>}
  </main>;
}

function price(amount: string, currency: string): string {
  const [integer, fraction = ""] = amount.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  const meaningfulFraction = fraction.replace(/0+$/, "");
  return `${currency === "PYG" ? "Gs." : currency} ${grouped}${meaningfulFraction ? `,${meaningfulFraction}` : ""}`;
}

export function CatalogHistoryDetailPage() {
  const { buildId } = useParams<{ buildId: string }>();
  const [detail, setDetail] = useState<CatalogHistoryDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!buildId) return;
    const controller = new AbortController();
    setLoading(true);
    setDetail(null);
    setError(null);
    fetchCatalogHistoryDetail(buildId, controller.signal)
      .then((value) => { setDetail(value); setLoading(false); })
      .catch((cause) => {
        if (!controller.signal.aborted) {
          setError(cause instanceof ApiError && cause.status === 404 ? "Catalog not found." : "Could not load catalog history.");
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [buildId]);

  return <main className="history-shell history-detail">
    <Link className="history-back" to="/catalogs">← All catalogs</Link>
    {loading && <div className="history-message" role="status">Loading catalog…</div>}
    {error && <div className="history-message history-error" role="alert">{error} <Link to="/catalogs">Back to catalogs</Link></div>}
    {!loading && detail && <>
      <header className="history-header"><div><p className="eyebrow">Frozen catalog record</p><h1>{detail.cover.title || "Catalog"}</h1><p>{detail.publisher?.display_name ?? "Historical data unavailable"} · {dateTime(detail.created_at)}</p></div><span className={`history-status history-${detail.status}`}>{statusLabel(detail.status)}</span></header>
      <div className="history-detail-actions"><PdfActions item={detail} /></div>
      {!detail.historical_data_available && <div className="history-message history-error" role="status">Some frozen historical data is unavailable or failed validation. This record has not been changed.</div>}
      {detail.error && <div className="history-message history-error" role="status">{detail.error}</div>}
      <section className="history-panel"><h2>Overview</h2><dl className="history-facts"><div><dt>Status</dt><dd>{statusLabel(detail.status)}</dd></div><div><dt>Created</dt><dd>{dateTime(detail.created_at)}</dd></div><div><dt>Completed</dt><dd>{dateTime(detail.completed_at)}</dd></div><div><dt>Products</dt><dd>{detail.product_count ?? "Unavailable"}</dd></div><div><dt>Pages</dt><dd>{detail.page_count ?? "Unavailable"}</dd></div><div><dt>Render</dt><dd>{detail.render_version}</dd></div><div><dt>Snapshot</dt><dd>{detail.snapshot_schema_version ?? "Unavailable"}</dd></div><div><dt>As of</dt><dd>{dateTime(detail.as_of)}</dd></div></dl></section>
      <section className="history-panel"><h2>Presentation</h2><dl className="history-facts"><div><dt>Publisher</dt><dd>{detail.publisher?.display_name ?? "Unavailable"}{detail.publisher?.logo_present ? " · Logo" : ""}{detail.publisher?.contact_text ? ` · ${detail.publisher.contact_text}` : ""}{detail.publisher?.social_handle ? ` · ${detail.publisher.social_handle}` : ""}</dd></div><div><dt>Layout</dt><dd>{detail.layout_display_name ?? "Unavailable"}{detail.layout_version ? ` v${detail.layout_version}` : ""}</dd></div><div><dt>Theme</dt><dd>{detail.theme_display_name ?? "Not available in this render version"}</dd></div><div><dt>Palette</dt><dd>{detail.palette_source === "custom" ? "Custom" : detail.palette_source === "publisher" ? "Publisher colors" : "Not available"}{detail.primary_color && detail.accent_color ? ` · ${detail.primary_color} / ${detail.accent_color}` : ""}</dd></div><div><dt>Cover</dt><dd>{featureLabel(detail.cover)}{detail.cover.title ? ` · ${detail.cover.title}` : ""}{detail.cover.subtitle ? ` · ${detail.cover.subtitle}` : ""}{detail.cover.edition_label ? ` · ${detail.cover.edition_label}` : ""}{detail.cover.hero_present ? " · Hero image" : ""}{detail.cover.show_publisher_logo ? " · Publisher logo" : ""}</dd></div><div><dt>Closing</dt><dd>{featureLabel(detail.closing)}{detail.closing.heading ? ` · ${detail.closing.heading}` : ""}{detail.closing.note ? ` · ${detail.closing.note}` : ""}{detail.closing.contacts.length ? ` · ${detail.closing.contacts.map((contact) => `${contact.kind.replaceAll("_", " ")}: ${contact.value}`).join("; ")}` : ""}{detail.closing.qr_target_type ? ` · QR: ${detail.closing.qr_target_type}` : ""}{detail.closing.qr_target_url ? ` · ${detail.closing.qr_target_url}` : ""}{detail.closing.show_publisher_logo ? " · Publisher logo" : ""}</dd></div></dl></section>
      <section className="history-panel"><h2>Products in this snapshot</h2>{detail.products.length === 0 ? <p>Historical product data unavailable.</p> : <div className="history-products">{detail.products.map((product, index) => <article key={`${product.product_name}-${index}`}><p className="section-kicker">{product.category_name} · {product.brand_name}</p><h3>{product.product_name}</h3>{product.short_description && <p>{product.short_description}</p>}<ul>{product.variants.map((variant, variantIndex) => <li key={`${variant.external_sku ?? "variant"}-${variantIndex}`}>{[variant.flavor, variant.size_value && variant.size_unit ? `${variant.size_value} ${variant.size_unit}` : null, variant.servings ? `${variant.servings} servings` : null].filter(Boolean).join(" · ") || variant.external_sku || "Variant"} — {price(variant.price_amount, variant.price_currency)}</li>)}</ul></article>)}</div>}</section>
      <section className="history-panel"><h2>Render attempts</h2>{detail.render_attempts.length === 0 ? <p>No render attempt recorded yet.</p> : <ol className="history-attempts">{detail.render_attempts.map((attempt) => <li key={attempt.attempt}><strong>Attempt {attempt.attempt} · {attempt.status}</strong><span>{dateTime(attempt.started_at)} → {dateTime(attempt.completed_at)}</span>{attempt.page_count && <span>{attempt.page_count} pages</span>}{attempt.error && <p>{attempt.error}</p>}</li>)}</ol>}</section>
      <section className="history-panel"><h2>Artifact</h2>{detail.artifact_available && detail.artifact ? <p>PDF · {detail.artifact.page_count} pages · created {dateTime(detail.artifact.created_at)} <PdfActions item={detail} /></p> : <p>Artifact unavailable. Preview and download are disabled.</p>}</section>
    </>}
  </main>;
}

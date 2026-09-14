from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.domain.schemas import CatalogRenderConfig


class CatalogRendererError(RuntimeError):
    pass


class RetryableCatalogRendererError(CatalogRendererError):
    pass


@dataclass(frozen=True)
class CatalogPdfRenderResult:
    pdf_bytes: bytes = field(repr=False)
    engine: str
    engine_version: str | None


class CatalogPdfRenderer(Protocol):
    def render(
        self,
        html: str,
        config: CatalogRenderConfig,
    ) -> CatalogPdfRenderResult: ...


def _default_playwright_factory():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


class ChromiumCatalogPdfRenderer:
    """Render trusted self-contained HTML using a local headless Chromium."""

    def __init__(
        self,
        *,
        playwright_factory: Callable[[], Any] = _default_playwright_factory,
    ) -> None:
        self._playwright_factory = playwright_factory

    def render(
        self,
        html: str,
        config: CatalogRenderConfig,
    ) -> CatalogPdfRenderResult:
        browser = None
        context = None
        page = None
        try:
            with self._playwright_factory() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(offline=True)
                    page = context.new_page()
                    page.set_content(html, wait_until="load")
                    pdf_bytes = page.pdf(
                        print_background=True,
                        prefer_css_page_size=True,
                        display_header_footer=False,
                    )
                    engine_version = browser.version
                finally:
                    for resource in (page, context, browser):
                        if resource is not None:
                            try:
                                resource.close()
                            except Exception:
                                pass
            return CatalogPdfRenderResult(
                pdf_bytes=pdf_bytes,
                engine="chromium",
                engine_version=engine_version,
            )
        except Exception:
            raise RetryableCatalogRendererError(
                "Chromium catalog rendering failed"
            ) from None

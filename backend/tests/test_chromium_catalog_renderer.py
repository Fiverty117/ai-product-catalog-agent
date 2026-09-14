import pytest

from app.domain.schemas import CatalogRenderConfig
from app.rendering.catalog_pdf import (
    ChromiumCatalogPdfRenderer,
    RetryableCatalogRendererError,
)


class FakePage:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.content_calls = []
        self.pdf_calls = []
        self.closed = False

    def set_content(self, html, **options):
        self.content_calls.append((html, options))

    def pdf(self, **options):
        self.pdf_calls.append(options)
        if self.failure:
            raise self.failure
        return b"%PDF-fake"

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    version = "123.4"

    def __init__(self, page):
        self.context = FakeContext(page)
        self.context_calls = []
        self.closed = False

    def new_context(self, **options):
        self.context_calls.append(options)
        return self.context

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.launch_calls = []

    def launch(self, **options):
        self.launch_calls.append(options)
        return self.browser


class FakePlaywrightManager:
    def __init__(self, playwright):
        self.playwright = playwright
        self.exited = False

    def __enter__(self):
        return self.playwright

    def __exit__(self, *args):
        self.exited = True


def renderer_fixture(*, failure=None):
    page = FakePage(failure=failure)
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    playwright = type("FakePlaywright", (), {"chromium": chromium})()
    manager = FakePlaywrightManager(playwright)
    renderer = ChromiumCatalogPdfRenderer(playwright_factory=lambda: manager)
    return renderer, page, browser, chromium, manager


def test_chromium_adapter_is_offline_and_uses_print_settings() -> None:
    renderer, page, browser, chromium, manager = renderer_fixture()

    result = renderer.render("<html>prepared</html>", CatalogRenderConfig())

    assert result.pdf_bytes == b"%PDF-fake"
    assert result.engine == "chromium"
    assert result.engine_version == "123.4"
    assert chromium.launch_calls == [{"headless": True}]
    assert browser.context_calls == [{"offline": True}]
    assert page.content_calls == [
        ("<html>prepared</html>", {"wait_until": "load"})
    ]
    assert page.pdf_calls == [
        {
            "print_background": True,
            "prefer_css_page_size": True,
            "display_header_footer": False,
        }
    ]
    assert page.closed and browser.context.closed and browser.closed
    assert manager.exited


def test_chromium_adapter_closes_resources_and_classifies_crash() -> None:
    renderer, page, browser, _, manager = renderer_fixture(
        failure=RuntimeError("Chromium crashed with sensitive diagnostics")
    )

    with pytest.raises(
        RetryableCatalogRendererError,
        match="Chromium catalog rendering failed",
    ) as exc_info:
        renderer.render("<html>prepared</html>", CatalogRenderConfig())

    assert "sensitive" not in str(exc_info.value)
    assert page.closed and browser.context.closed and browser.closed
    assert manager.exited

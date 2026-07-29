"""Persistent class/factory fixture for the unified session sandbox."""

from playwright.sync_api import sync_playwright
from selenium import webdriver


class BrowserProbe:
    """Launch one real headed browser for each explicit probe method."""

    def __init__(self, engine: str = "playwright") -> None:
        if engine not in {"playwright", "selenium"}:
            raise ValueError("engine must be playwright or selenium")
        self.engine = engine

    def title(self, expected: str) -> str:
        if self.engine == "playwright":
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                try:
                    page = browser.new_page()
                    page.set_content(f"<title>{expected}</title>")
                    return page.title()
                finally:
                    browser.close()

        options = webdriver.ChromeOptions()
        options.binary_location = "/usr/bin/chromium"
        driver = webdriver.Chrome(options=options)
        try:
            driver.get(f"data:text/html,<title>{expected}</title>")
            return driver.title
        finally:
            driver.quit()

    def close(self) -> None:
        """Expose the conventional persistent-object cleanup method."""


def open_browser_probe(engine: str = "playwright") -> BrowserProbe:
    """Return a persistent probe through the exact public factory contract."""

    return BrowserProbe(engine)

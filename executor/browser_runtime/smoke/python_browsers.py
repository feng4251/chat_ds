"""Real-browser smoke for both Python Playwright and Selenium."""

import sys

from playwright.sync_api import sync_playwright
from selenium import webdriver


if len(sys.argv) == 3:
    network_url, network_title = sys.argv[1:]
elif len(sys.argv) == 1:
    network_url = None
    network_title = "chatds-network-smoke"
else:
    raise AssertionError("expected no args or network URL/title")

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=False)
    try:
        page = browser.new_page()
        page.set_content("<title>chatds-python-playwright-smoke</title>")
        assert page.title() == "chatds-python-playwright-smoke"
        if network_url:
            page.goto(network_url, wait_until="domcontentloaded")
            assert page.title() == network_title
    finally:
        browser.close()

options = webdriver.ChromeOptions()
options.binary_location = "/usr/bin/chromium"
# Exercise Selenium's ordinary Service() path as exact Skills do. The baked-in
# matching driver must be discovered locally while SE_OFFLINE forbids download.
driver = webdriver.Chrome(options=options)
try:
    driver.get("data:text/html,<title>chatds-selenium-smoke</title>")
    assert driver.title == "chatds-selenium-smoke"
    if network_url:
        driver.get(network_url)
        assert driver.title == network_title
finally:
    driver.quit()

print("python-playwright-selenium-ok")

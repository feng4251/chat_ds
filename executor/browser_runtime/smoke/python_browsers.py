"""Real-browser smoke for both Python Playwright and Selenium."""

from playwright.sync_api import sync_playwright
from selenium import webdriver


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=False)
    try:
        page = browser.new_page()
        page.set_content("<title>chatds-python-playwright-smoke</title>")
        assert page.title() == "chatds-python-playwright-smoke"
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
finally:
    driver.quit()

print("python-playwright-selenium-ok")

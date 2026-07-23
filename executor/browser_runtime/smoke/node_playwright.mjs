import { chromium } from "playwright";

const browser = await chromium.launch({ headless: false });
try {
  const page = await browser.newPage();
  await page.setContent("<title>chatds-node-playwright-esm-smoke</title>");
  if ((await page.title()) !== "chatds-node-playwright-esm-smoke") {
    throw new Error("unexpected ESM Playwright page title");
  }
} finally {
  await browser.close();
}

console.log("node-playwright-esm-ok");

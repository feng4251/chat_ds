import { chromium } from "playwright";

if (![2, 4].includes(process.argv.length)) {
  throw new Error("expected no args or network URL/title");
}
const networkURL = process.argv[2];
const networkTitle = process.argv[3] || "chatds-network-smoke";
const browser = await chromium.launch({ headless: false });
try {
  const page = await browser.newPage();
  await page.setContent("<title>chatds-node-playwright-esm-smoke</title>");
  if ((await page.title()) !== "chatds-node-playwright-esm-smoke") {
    throw new Error("unexpected ESM Playwright page title");
  }
  if (networkURL) {
    await page.goto(networkURL, { waitUntil: "domcontentloaded" });
    if ((await page.title()) !== networkTitle) {
      throw new Error("unexpected network smoke page title");
    }
  }
} finally {
  await browser.close();
}

console.log("node-playwright-esm-ok");

"use strict";

const { chromium } = require("playwright");

(async () => {
  if (![2, 4].includes(process.argv.length)) {
    throw new Error("expected no args or network URL/title");
  }
  const networkURL = process.argv[2];
  const networkTitle = process.argv[3] || "chatds-network-smoke";
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage();
    await page.setContent("<title>chatds-node-playwright-smoke</title>");
    if ((await page.title()) !== "chatds-node-playwright-smoke") {
      throw new Error("unexpected Playwright page title");
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
  process.stdout.write("node-playwright-ok\n");
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});

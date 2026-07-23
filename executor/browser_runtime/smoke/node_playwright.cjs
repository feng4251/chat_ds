"use strict";

const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage();
    await page.setContent("<title>chatds-node-playwright-smoke</title>");
    if ((await page.title()) !== "chatds-node-playwright-smoke") {
      throw new Error("unexpected Playwright page title");
    }
  } finally {
    await browser.close();
  }
  process.stdout.write("node-playwright-ok\n");
})().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});

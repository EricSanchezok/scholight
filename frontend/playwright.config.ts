import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: "html",
  expect: {
    toHaveScreenshot: {
      maxDiffPixelRatio: 0.001,
    },
  },
  use: {
    baseURL: "http://127.0.0.1:7200",
    locale: "en-US",
    timezoneId: "Asia/Shanghai",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["iPhone 13"], browserName: "chromium" } },
  ],
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:7200",
    reuseExistingServer: !process.env.CI,
  },
});

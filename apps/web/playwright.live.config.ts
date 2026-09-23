import { defineConfig, devices } from "@playwright/test";
import { readFileSync } from "node:fs";

const ready = JSON.parse(readFileSync("output/live-acceptance/fictional-ready.json", "utf8"));
if (ready.fictional !== true || new URL(ready.frontendOrigin).hostname !== "127.0.0.1")
  throw new Error("Live acceptance requires the owned fictional localhost fixture.");

export default defineConfig({
  testDir: "./live-e2e",
  outputDir: "./output/live-test-results",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  timeout: 180_000,
  expect: { timeout: 20_000 },
  use: {
    ...devices["Desktop Chrome"], baseURL: ready.frontendOrigin,
    viewport: { width: 1440, height: 900 }, actionTimeout: 15_000, trace: "retain-on-failure",
  },
});

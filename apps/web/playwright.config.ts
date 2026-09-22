import { createServer } from "node:net";
import { defineConfig, devices } from "@playwright/test";

/** Ask the OS for a free localhost port so parallel worktrees never collide. */
async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      server.close(() => resolve(typeof address === "object" && address ? address.port : 0));
    });
  });
}

const port = Number(process.env.IMX_WEB_TEST_PORT) || (await freePort());
process.env.IMX_WEB_TEST_PORT = String(port);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./output/test-results",
  fullyParallel: true,
  reporter: [["list"]],
  timeout: 45_000,
  use: { baseURL, trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    // Production build; no IMX_BACKEND_URL, so the live desk must report the service as unavailable.
    command: `npx next build && npx next start --hostname 127.0.0.1 --port ${port}`,
    url: `${baseURL}/preview`,
    timeout: 180_000,
    reuseExistingServer: false,
    env: { IMX_BACKEND_URL: "", NEXT_TELEMETRY_DISABLED: "1" },
  },
});

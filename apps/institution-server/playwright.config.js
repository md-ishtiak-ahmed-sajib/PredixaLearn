import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const windowsPython = "..\\..\\.venv\\Scripts\\python.exe";
const python = process.env.PREDIXALEARN_PYTHON || (existsSync(windowsPython) ? windowsPython : "python");
const port = process.env.PREDIXALEARN_INSTITUTION_TEST_PORT || "8101";
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests/web",
  timeout: 30_000,
  use: { baseURL, viewport: { width: 1280, height: 720 } },
  webServer: {
    command: `${python} -m uvicorn institution_server.main:app --port ${port}`,
    url: `${baseURL}/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      PREDIXALEARN_INSTITUTION_ENV: "test",
      PREDIXALEARN_DATABASE_URL: "sqlite:///./test-results/playwright.sqlite3",
      PREDIXALEARN_AUTH_MODE: "test",
      PREDIXALEARN_TEST_JWT_SECRET: "playwright-secret-at-least-32-bytes",
      PREDIXALEARN_STORAGE_ROOT: resolve("test-results", "storage"),
    },
  },
});

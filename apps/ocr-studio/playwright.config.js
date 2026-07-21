import { defineConfig } from "@playwright/test";
import { existsSync } from "node:fs";
import { resolve } from "node:path";

const localPython = ".venv\\Scripts\\python.exe";
const compatibilityPython = "..\\..\\.venv\\Scripts\\python.exe";
const python =
  process.env.PREDIXALEARN_PYTHON ||
  (existsSync(localPython) ? localPython : compatibilityPython);
const isolatedPort = process.env.PREDIXALEARN_TEST_PORT;
const port = isolatedPort || "8131";
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests/web",
  timeout: 30_000,
  use: {
    baseURL,
    viewport: { width: 1280, height: 720 },
  },
  webServer: {
    command: `${python} run.py --no-browser`,
    url: `${baseURL}/`,
    // A distinct port is useful for CI and for local tests while a user keeps
    // the interactive studio open on its normal development port.
    reuseExistingServer: process.env.PREDIXALEARN_TEST_REUSE === "1",
    timeout: 120_000,
    env: {
      OCR_DEVICE: "cpu",
      OCR_PORT: port,
      OCR_LOG_DIR: resolve("test-results", "playwright-runtime", port, "logs"),
      PREDIXALEARN_TOOLS_ROOT: process.env.PREDIXALEARN_TOOLS_ROOT || "..\\..\\.tools",
    },
  },
});

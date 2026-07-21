import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const sums = await readFile(resolve(root, "web/static/vendor/SHA256SUMS"), "utf8");
for (const line of sums.trim().split(/\r?\n/)) {
  const [expected, relative] = line.trim().split(/\s+/, 2);
  const bytes = await readFile(resolve(root, relative));
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== expected) throw new Error(`Checksum mismatch: ${relative}`);
}
const requiredLicenses = [
  "web/static/fonts/MANROPE-LICENSE.txt",
  "web/static/vendor/licenses/DOMPURIFY-LICENSE.txt",
  "web/static/vendor/licenses/MARKED-LICENSE.txt",
  "web/static/vendor/licenses/PDFJS-LICENSE.txt",
  "web/static/vendor/licenses/SWAGGER-UI-LICENSE.txt",
];
for (const relative of requiredLicenses) {
  const text = await readFile(resolve(root, relative), "utf8");
  if (!text.trim()) throw new Error(`Missing or empty license: ${relative}`);
}
console.log(`Verified ${sums.trim().split(/\r?\n/).length} vendored assets and licenses.`);

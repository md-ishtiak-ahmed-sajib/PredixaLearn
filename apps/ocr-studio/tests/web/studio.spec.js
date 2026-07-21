import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("Home is task-first, keyboard-accessible, and has no severe axe findings", async ({ page }) => {
  test.slow();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Turn past-paper scans into trustworthy knowledge." })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Exam Paper Recommended Questions, marks, tables, figures, and source-linked review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run Judge Demo" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Convert document" })).toBeDisabled();
  await page.getByRole("tab", { name: "Advanced Choose specialist OCR strategies for English documents" }).click();
  await expect(page.getByText("OCR language English")).toBeVisible();
  await expect(page.locator("#ocr-language")).toHaveValue("en");
  await page.getByRole("tab", { name: "Vision-Language Advanced understanding for unusually complex English pages" }).click();
  await expect(page.locator("#document-strategy")).toBeDisabled();
  const results = await new AxeBuilder({ page }).analyze();
  const severe = results.violations.filter((item) => ["serious", "critical"].includes(item.impact));
  expect(severe).toEqual([]);
});

test("Home, Tips, and History do not overflow at a phone width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of ["/", "/tips", "/history", "/analyze", "/benchmark", "/review", "/teacher", "/revision"]) {
    await page.goto(route);
    const widths = await page.locator("html").evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }));
    expect(widths.scrollWidth).toBe(widths.clientWidth);
  }
});

test("History loads through its ES module without browser errors", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/history");
  await expect(page.locator("#history-count")).not.toHaveText("Loading history…");
  expect(errors).toEqual([]);
});

test("Upload, process, progress, result, and reset controls keep their workflow hooks", async ({ page }) => {
  const jobId = "a".repeat(32);
  await page.route("**/api/v1/ocr/**", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ job_id: jobId, status: "pending" }),
    });
  });
  await page.route("**/api/v1/jobs/**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        job_id: jobId,
        status: "completed",
        progress: { stage: "completed", message: "Complete", total_pages: 1, completed_pages: 1 },
        result: { full_text: "Mocked OCR text", markdown: "# Mocked OCR text", page_count: 1 },
      }),
    });
  });
  await page.goto("/");
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("predixalearn:health", {
    detail: { status: "ready", warmup: { status: "ready" } },
  })));
  await page.locator("#file-input").setInputFiles({
    name: "scan.png",
    mimeType: "image/png",
    buffer: Buffer.from([137, 80, 78, 71]),
  });
  await expect(page.locator("#file-card")).toBeVisible();
  await expect(page.getByRole("button", { name: "Convert document" })).toBeEnabled();
  await page.getByRole("button", { name: "Convert document" }).click();
  await expect(page.locator("#result-title")).toHaveText("Processing complete");
  await expect(page.locator("#result-output")).toContainText("Mocked OCR text");
  await page.getByRole("button", { name: "Process another" }).click();
  await expect(page.locator("#file-card")).toBeHidden();
});

test("Judge Demo follows the normal queue and opens the source-linked Analyze handoff", async ({ page }) => {
  const jobId = "d".repeat(32);
  await page.route("**/api/v1/demo/judge", async (route) => {
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ job_id: jobId, status: "pending", document_profile: "exam" }),
    });
  });
  await page.route("**/api/v1/jobs/**", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        job_id: jobId,
        status: "completed",
        progress: { stage: "completed", message: "Complete", total_pages: 2, completed_pages: 2 },
        result: { full_text: "1(a) Demo question (5 marks)", page_count: 2, document_profile: "exam" },
      }),
    });
  });
  await page.goto("/");
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("predixalearn:health", {
    detail: { status: "ready", warmup: { status: "ready" } },
  })));
  await page.getByRole("button", { name: "Run Judge Demo" }).click();
  await expect(page.locator("#result-title")).toHaveText("Processing complete");
  await expect(page.locator("#open-analysis")).toBeVisible();
  await expect(page.locator("#open-analysis")).toHaveAttribute("href", `/analyze?job=${jobId}`);
});

test("Analyze keeps GPT consent separate from local source review", async ({ page }) => {
  const jobId = "e".repeat(32);
  const analysis = {
    document_id: jobId,
    updated_at: "2026-07-21T00:00:00+00:00",
    model: { status: "local_evidence", consent_to_cloud: false, model: null },
    questions: [{
      question_id: "q-1-a",
      display_number: "1(a)",
      text: "Explain continuity.",
      source_page: 1,
      source_line_ids: ["p0001-l00001"],
      source_bbox: [0.1, 0.2, 0.7, 0.32],
      ocr_confidence: 0.94,
      marks: 5,
      ai: { classification_confidence: null },
    }],
    practice_items: [],
    analysis: { question_count: 1, explicit_marks_total: 5, revision_priorities: [], recurring_topics: [] },
  };
  await page.route("**/api/v1/history?*", async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      items: [{ job_id: jobId, input_name: "synthetic-exam.pdf", document_profile: "exam" }], total: 1,
    }) });
  });
  await page.route("**/api/v1/history/**", async (route) => {
    if (route.request().url().includes("/analysis")) {
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(analysis) });
      return;
    }
    await route.fallback();
  });
  await page.route(`**/api/v1/history/${jobId}/source-pages/1`, async (route) => {
    await route.fulfill({
      contentType: "image/png",
      body: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9JtNwAAAAASUVORK5CYII=", "base64"),
    });
  });
  await page.route(`**/api/v1/history/${jobId}/analysis/practice`, async (route) => {
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({
      document_id: jobId,
      consent_to_cloud: false,
      review_required: true,
      items: [{ source_question_id: "q-1-a", prompt: "Teacher review required: draft a parallel question.", review_status: "needs_teacher_review" }],
    }) });
  });
  await page.goto(`/analyze?job=${jobId}`);
  await expect(page.getByRole("heading", { name: "1(a)" })).toBeVisible();
  await expect(page.locator(".source-highlight")).toBeVisible();
  await expect(page.getByRole("button", { name: "Analyze with GPT-5.6" })).toBeDisabled();
  await page.locator("#analysis-consent").check();
  await expect(page.getByRole("button", { name: "Analyze with GPT-5.6" })).toBeEnabled();
  await page.getByRole("button", { name: "Generate local practice starters" }).click();
  await expect(page.locator("#analysis-practice-list")).toContainText("parallel question");
  await page.getByLabel("1(a) topic").fill("Continuity");
  await page.getByRole("button", { name: "Save teacher review" }).click();
  await expect(page.locator("#analysis-status")).toContainText("teacher review saved locally");
});

test("Maintenance dialog reviews signed channel results before enabling apply", async ({ page }) => {
  await page.route("**/api/v1/maintenance/check", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        channel: { status: "available", manifest_version: "runtime-1", message: "Signed maintenance release verified." },
        inventory: [{ id: "paddleocr", name: "PaddleOCR", installed_version: "3.7.0", health: "ready", detail: "Approved OCR runtime dependency." }],
        ready_to_update: [{ id: "paddle-runtime", name: "Approved Paddle runtime", installed_version: "3.7.0", target_version: "3.7.1" }],
        blocked: [{ id: "unsafe", name: "Unapproved tool", installed_version: "1", target_version: "2", reason: "No approved handler." }],
        review_token: "a".repeat(64),
        can_apply: true,
      }),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Maintenance" }).click();
  await expect(page.getByRole("dialog", { name: "Runtime maintenance" })).toBeVisible();
  await expect(page.getByText("Maintenance in progress: We are updating our systems and appreciate your patience.")).toBeVisible();
  await expect(page.locator("#maintenance-inventory")).toContainText("PaddleOCR");
  await expect(page.locator("#maintenance-ready")).toContainText("3.7.1");
  await expect(page.locator("#maintenance-blocked")).toContainText("No approved handler.");
  await expect(page.locator("#maintenance-progress")).toBeVisible();
  await expect(page.locator("#maintenance-progress-bar")).toHaveAttribute("value", "100");
  await expect(page.locator("#maintenance-progress-stage")).toHaveText("Signed channel check complete.");
  await expect(page.getByRole("button", { name: "Apply reviewed updates" })).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Runtime maintenance" })).toBeHidden();
});

test("Maintenance check shows indeterminate progress until the signed response arrives", async ({ page }) => {
  let releaseCheck;
  const checkPending = new Promise((resolve) => { releaseCheck = resolve; });
  await page.route("**/api/v1/maintenance/check", async (route) => {
    await checkPending;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        channel: { status: "available", manifest_version: "runtime-1", message: "Signed maintenance release verified." },
        inventory: [],
        ready_to_update: [],
        blocked: [],
        can_apply: false,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Maintenance" }).click();
  await expect(page.locator("#maintenance-progress")).toBeVisible();
  await expect(page.locator("#maintenance-progress")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#maintenance-progress-bar")).not.toHaveAttribute("value", /\d+/);
  await expect(page.getByRole("button", { name: "Check again" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Apply reviewed updates" })).toBeDisabled();

  releaseCheck();
  await expect(page.locator("#maintenance-progress-bar")).toHaveAttribute("value", "100");
  await expect(page.locator("#maintenance-progress")).toHaveAttribute("aria-busy", "false");
});

test("Maintenance apply updates live progress and preserves restart-required state", async ({ page }) => {
  await page.route("**/api/v1/maintenance/check", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        channel: { status: "available", manifest_version: "runtime-1", message: "Signed maintenance release verified." },
        inventory: [],
        ready_to_update: [{ id: "paddle-runtime", name: "Approved Paddle runtime", installed_version: "3.7.0", target_version: "3.7.1" }],
        blocked: [],
        review_token: "a".repeat(64),
        can_apply: true,
      }),
    });
  });
  await page.route("**/api/v1/maintenance/apply", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ job_id: "b".repeat(32), status: "queued", stage: "queued", progress: 0, cancellable: true }),
    });
  });
  let polls = 0;
  await page.route("**/api/v1/maintenance/jobs/**", async (route) => {
    polls += 1;
    const isRestartReady = polls > 1;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(isRestartReady ? {
        job_id: "b".repeat(32),
        status: "restart_required",
        stage: "restart",
        progress: 90,
        cancellable: false,
        message: "Approved files passed staging validation. Close and reopen PredixaLearn to finish safely.",
      } : {
        job_id: "b".repeat(32),
        status: "downloading",
        stage: "download",
        progress: 55,
        cancellable: true,
        message: "Downloading approved files",
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Maintenance" }).click();
  await expect(page.getByRole("button", { name: "Apply reviewed updates" })).toBeEnabled();
  await page.getByRole("button", { name: "Apply reviewed updates" }).click();
  await expect(page.locator("#maintenance-progress-bar")).toHaveAttribute("value", "55");
  await expect(page.locator("#maintenance-progress-stage")).toHaveText("Downloading approved updates.");
  await expect(page.getByRole("button", { name: "Cancel maintenance" })).toBeVisible();
  await expect(page.locator("#maintenance-progress-bar")).toHaveAttribute("value", "90", { timeout: 3000 });
  await expect(page.locator("#maintenance-progress-stage")).toHaveText("Restart required to complete maintenance.");
  await expect(page.locator("#maintenance-progress")).toHaveClass(/is-warning/);
  await expect(page.getByRole("button", { name: "Cancel maintenance" })).toBeHidden();
});

test("Maintenance is unavailable when the browser goes offline", async ({ page }) => {
  await page.goto("/");
  const maintenance = page.getByRole("button", { name: "Maintenance" });
  await expect(maintenance).toBeEnabled();
  await page.context().setOffline(true);
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));
  await expect(maintenance).toBeDisabled();
  await page.context().setOffline(false);
});

test("Desktop Quit uses the launch control token and keeps active OCR work safe", async ({ page }) => {
  let active = true;
  await page.route("**/api/v1/app/quit", async (route) => {
    expect(route.request().headers()["x-predixalearn-control"]).toBe("t".repeat(32));
    await route.fulfill(active ? {
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Finish or cancel active OCR work before quitting PredixaLearn." }),
    } : {
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ status: "shutting_down" }),
    });
  });
  await page.goto("/");
  await page.evaluate(() => {
    document.head.insertAdjacentHTML("beforeend", '<meta name="predixalearn-control-token" content="tttttttttttttttttttttttttttttttt">');
    document.body.insertAdjacentHTML("beforeend", `
      <button id="quit-app-button" type="button">Quit app</button>
      <dialog id="quit-app-dialog">
        <button id="quit-app-close" type="button">Keep working</button>
        <button id="quit-app-cancel" type="button">Cancel</button>
        <button id="quit-app-confirm" type="button">Quit PredixaLearn</button>
        <p id="quit-app-status"></p>
      </dialog>`);
  });
  await page.addScriptTag({ url: "/static/desktop.js" });
  await page.getByRole("button", { name: "Quit app" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Quit PredixaLearn" }).click();
  await expect(page.locator("#quit-app-status")).toContainText("Finish or cancel active OCR work");
  active = false;
  await page.getByRole("button", { name: "Quit PredixaLearn" }).click();
  await expect(page.locator("#quit-app-status")).toContainText("PredixaLearn is closing");
  await expect(page.getByRole("button", { name: "Quit app" })).toBeDisabled();
});

test("Review links source geometry, reconstructed text, and accessible issue navigation", async ({ page }) => {
  test.slow();
  const jobId = "e".repeat(32);
  await page.route("**/api/v1/history?**", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [{ job_id: jobId, input_name: "exam.pdf", language: "en" }], total: 1 }) }));
  await page.route(`**/api/v1/history/${jobId}/review-workspace`, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    job_id: jobId, input_name: "exam.pdf", source_hash: "f".repeat(64), language: "en", approved_correction_count: 0,
    pages: [{ page: 1, source_preview_url: "/static/predixalearn-mark-180.png", lines: [{ line_id: "p1-l1", text: "1. Explain evidence.", raw_text: "1. Explain evidence.", confidence: 0.61, confidence_band: "low", corrected: false, normalized_bbox: [0.1, 0.1, 0.8, 0.2], page_number: 1 }] }],
    questions: [{ question_id: "q-1", display_number: "1", text: "Explain evidence.", source_page: 1 }], reconstructed_text: "1. Explain evidence.",
    screen_reader_issues: [{ issue_id: "issue-p1-l1", line_id: "p1-l1", page: 1, text: "1. Explain evidence.", confidence: 0.61, confidence_band: "low", correction_status: "uncorrected", geometry_available: true }],
  }) }));
  await page.route(`**/api/v1/history/${jobId}/corrections/audit`, (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ job_id: jobId, events: [] }) }));
  await page.goto("/review", { waitUntil: "domcontentloaded" });
  await page.locator("#review-history").selectOption(jobId);
  await expect(page.locator(".confidence-region")).toHaveCount(1);
  await page.getByRole("button", { name: /low confidence region/i }).click();
  await expect(page.locator("#correction-original")).toHaveValue("1. Explain evidence.");
  await expect(page.locator("#issues-summary")).toHaveText("1 review issue(s), ordered by page.");
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((item) => ["serious", "critical"].includes(item.impact))).toEqual([]);
});

test("Teacher and Revision workspaces load safe local dashboard states", async ({ page }) => {
  await page.route("**/api/v1/dashboards/teacher", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ papers_awaiting_review: [], pending_corrections: 1, low_confidence_jobs: 2, unmapped_questions: 3, question_bank_pending: 4, active_batches: 0 }) }));
  await page.route("**/api/v1/taxonomies", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [] }) }));
  await page.route("**/api/v1/syllabi", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [] }) }));
  await page.route("**/api/v1/question-bank?**", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [] }) }));
  await page.route("**/api/v1/batches", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [] }) }));
  await page.route("**/api/v1/languages/reliability", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ items: [{ language: "en", label: "English", ocr: "supported", question_segmentation: "supported", marks_extraction: "supported", taxonomy_mapping: "supported", duplicate_normalization: "supported", analysis: "supported_with_consent", export_fonts: "supported", right_to_left: false }] }) }));
  await page.goto("/teacher");
  await expect(page.locator("#teacher-metrics .metric-card")).toHaveCount(6);
  await expect(page.locator("#taxonomy-tree")).toContainText("Unmapped legacy topics");
  await expect(page.locator("#language-reliability")).toContainText("English");
  await expect(page.getByRole("heading", { name: "English-only processing" })).toBeVisible();
  await expect(page.locator("#batch-language")).toHaveValue("en");
  await expect(page.locator("#batch-remove-terms")).toBeVisible();
  await expect(page.locator("#bank-filter-query")).toBeVisible();

  await page.route("**/api/v1/revision-packs?audience=student", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ mode: "standalone_teacher_preview", student_accounts: false, items: [{ pack_id: "pack-1", title: "Mechanics", language: "en", source_item_ids: ["item-1"], payload: { introduction: "Review forces." } }] }) }));
  await page.goto("/revision");
  await expect(page.locator("#revision-packs")).toContainText("Mechanics");
  await expect(page.locator("#revision-packs")).toContainText("English · teacher-approved");
  await expect(page.locator("#revision-packs")).toContainText("teacher-approved");
  await expect(page.getByRole("link", { name: "Download MARKDOWN" })).toBeVisible();
});

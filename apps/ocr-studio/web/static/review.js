// @ts-check
export {};

const byId = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
const historySelect = /** @type {HTMLSelectElement} */ (byId("review-history"));
const statusNode = byId("review-status");
const workspaceNode = byId("review-workspace");
const sourceImage = /** @type {HTMLImageElement} */ (byId("review-source-image"));
const overlayNode = byId("review-overlay");
const linesNode = byId("review-lines");
const issuesNode = byId("review-issues");
const auditNode = byId("review-audit");
const form = /** @type {HTMLFormElement} */ (byId("correction-form"));
let workspace = null;
let pageIndex = 0;
let selectedLine = null;
let synchronizingScroll = false;

async function api(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const body = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body?.detail || `Request failed (${response.status})`);
  return body;
}

function setStatus(message, error = false) {
  statusNode.textContent = message;
  statusNode.classList.toggle("error-callout", error);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadHistory() {
  const data = await api("/api/v1/history?status=completed&limit=100");
  for (const item of data.items) {
    const option = document.createElement("option");
    option.value = item.job_id;
    const language = item.language === "en"
      ? "English"
      : "Legacy language record";
    option.textContent = `${item.input_name} · ${language}`;
    historySelect.append(option);
  }
  const requested = new URLSearchParams(location.search).get("job_id");
  if (requested && [...historySelect.options].some((option) => option.value === requested)) {
    historySelect.value = requested;
    await loadWorkspace();
  }
}

function currentPage() {
  return workspace?.pages?.[pageIndex] || null;
}

function selectLine(lineId) {
  const page = currentPage();
  const line = page?.lines?.find((value) => value.line_id === lineId);
  if (!line) return;
  selectedLine = line;
  document.querySelectorAll("[data-line-id]").forEach((node) => node.classList.toggle("selected", node.getAttribute("data-line-id") === lineId));
  /** @type {HTMLTextAreaElement} */ (byId("correction-original")).value = line.raw_text || line.text || "";
  /** @type {HTMLTextAreaElement} */ (byId("correction-replacement")).value = line.text || "";
  /** @type {HTMLInputElement} */ (byId("correction-line-id")).value = lineId;
  document.querySelector(`.review-line[data-line-id="${CSS.escape(lineId)}"]`)?.scrollIntoView({ block: "nearest", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

function renderOverlay(page) {
  overlayNode.replaceChildren();
  const enabled = new Set([...document.querySelectorAll("[data-band]:checked")].map((node) => node.getAttribute("data-band")));
  const enabledTypes = new Set([...document.querySelectorAll("[data-evidence-type]:checked")].map((node) => node.getAttribute("data-evidence-type")));
  for (const line of page.lines) {
    const bbox = line.normalized_bbox;
    if (!bbox || !enabled.has(line.confidence_band) || !(line.evidence_types || ["text"]).some((value) => enabledTypes.has(value))) continue;
    const button = element("button", `confidence-region band-${line.confidence_band}`);
    button.type = "button";
    button.dataset.lineId = line.line_id;
    button.style.left = `${bbox[0] * 100}%`;
    button.style.top = `${bbox[1] * 100}%`;
    button.style.width = `${(bbox[2] - bbox[0]) * 100}%`;
    button.style.height = `${(bbox[3] - bbox[1]) * 100}%`;
    button.title = `${line.confidence_band} confidence; ${(line.evidence_types || ["text"]).join(", ")}: ${line.text}`;
    button.setAttribute("aria-label", `${line.confidence_band} confidence region, ${(line.evidence_types || ["text"]).join(", ")}, ${line.confidence == null ? "score unavailable" : Math.round(line.confidence * 100) + " percent"}: ${line.text}`);
    button.addEventListener("click", () => selectLine(line.line_id));
    overlayNode.append(button);
  }
}

function renderLines(page) {
  linesNode.replaceChildren();
  for (const line of page.lines) {
    const row = element("button", `review-line band-${line.confidence_band}`);
    row.type = "button";
    row.dataset.lineId = line.line_id;
    const confidence = line.confidence == null ? "Confidence unavailable" : `${Math.round(line.confidence * 100)}% OCR confidence`;
    row.append(element("span", "review-line-text", line.text || "Empty OCR line"), element("small", "", `${confidence}${line.corrected ? " · approved correction" : ""}`));
    row.addEventListener("click", () => selectLine(line.line_id));
    linesNode.append(row);
  }
}

function renderIssues() {
  issuesNode.replaceChildren();
  for (const issue of workspace.screen_reader_issues) {
    const item = element("li");
    const button = element("button", "issue-button", `Page ${issue.page}: ${issue.text || "Empty line"}`);
    button.type = "button";
    button.append(element("small", "", `${issue.confidence == null ? "Confidence unavailable" : Math.round(issue.confidence * 100) + "% confidence"}; ${(issue.evidence_types || ["text"]).join(", ")}; ${issue.correction_status}; ${issue.geometry_available ? "geometry available" : "no geometry"}`));
    button.addEventListener("click", () => {
      pageIndex = Math.max(0, workspace.pages.findIndex((page) => page.page === issue.page));
      renderPage();
      selectLine(issue.line_id);
    });
    item.append(button);
    issuesNode.append(item);
  }
  byId("issues-summary").textContent = `${workspace.screen_reader_issues.length} review issue(s), ordered by page.`;
}

function renderPage() {
  const page = currentPage();
  if (!page) return;
  sourceImage.src = page.source_preview_url;
  sourceImage.alt = `Watermarked local source page ${page.page} of ${workspace.pages.length}`;
  byId("review-page-label").textContent = `Page ${pageIndex + 1} of ${workspace.pages.length}`;
  /** @type {HTMLButtonElement} */ (byId("review-prev")).disabled = pageIndex === 0;
  /** @type {HTMLButtonElement} */ (byId("review-next")).disabled = pageIndex >= workspace.pages.length - 1;
  selectedLine = null;
  renderOverlay(page);
  renderLines(page);
}

function renderQuestionOptions() {
  const select = /** @type {HTMLSelectElement} */ (byId("review-question")); select.replaceChildren(new Option("Choose a question", ""));
  for (const question of workspace.questions || []) select.append(new Option(`${question.display_number}: ${question.text}`.slice(0, 180), question.question_id));
  select.disabled = !workspace.questions?.length;
  /** @type {HTMLSelectElement} */ (byId("review-source-mode")).disabled = !workspace.questions?.length;
}

async function loadAudit() {
  auditNode.replaceChildren();
  if (!workspace) return;
  const data = await api(`/api/v1/history/${workspace.job_id}/corrections/audit`);
  for (const event of data.events.slice().reverse()) {
    const item = element("li", "audit-event");
    item.append(element("strong", "", event.action.replaceAll("_", " ")), element("span", "", `${event.actor} · ${new Date(event.created_at).toLocaleString()}`));
    auditNode.append(item);
  }
  if (!data.events.length) auditNode.append(element("li", "empty-copy", "No correction events yet."));
}

async function loadWorkspace() {
  if (!historySelect.value) return;
  setStatus("Loading immutable evidence and teacher overlays…");
  try {
    workspace = await api(`/api/v1/history/${historySelect.value}/review-workspace`);
    pageIndex = 0;
    workspaceNode.classList.remove("hidden");
    byId("review-effective-count").textContent = `${workspace.approved_correction_count} approved correction(s)`;
    for (const [id, format] of [["review-download-markdown", "markdown"], ["review-download-json", "json"], ["review-download-docx", "docx"]]) { const link = /** @type {HTMLAnchorElement} */ (byId(id)); link.href = `/api/v1/history/${workspace.job_id}/corrected/download?format=${format}`; link.classList.remove("hidden"); }
    renderPage();
    renderQuestionOptions();
    renderIssues();
    await loadAudit();
    setStatus(`${workspace.input_name} loaded. OCR hash ${workspace.source_hash.slice(0, 12)}… remains immutable.`);
  } catch (error) {
    setStatus(error.message, true);
  }
}

historySelect.addEventListener("change", loadWorkspace);
byId("review-prev").addEventListener("click", () => { if (pageIndex > 0) { pageIndex -= 1; renderPage(); } });
byId("review-next").addEventListener("click", () => { if (workspace && pageIndex < workspace.pages.length - 1) { pageIndex += 1; renderPage(); } });
document.querySelectorAll("[data-band]").forEach((node) => node.addEventListener("change", () => currentPage() && renderOverlay(currentPage())));
document.querySelectorAll("[data-evidence-type]").forEach((node) => node.addEventListener("change", () => currentPage() && renderOverlay(currentPage())));
byId("review-zoom").addEventListener("input", (event) => {
  const value = /** @type {HTMLInputElement} */ (event.currentTarget).value;
  byId("review-zoom-output").textContent = `${value}%`;
  byId("review-source-stage").style.setProperty("--review-zoom", String(Number(value) / 100));
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!workspace) return;
  const targetKind = /** @type {HTMLSelectElement} */ (byId("correction-target-kind")).value;
  const targetId = /** @type {HTMLInputElement} */ (byId("correction-line-id")).value.trim();
  if (!targetId) return setStatus("Select evidence or enter a valid correction target ID.", true);
  const rawReplacement = /** @type {HTMLTextAreaElement} */ (byId("correction-replacement")).value;
  const replacement = targetKind === "marks" ? { marks: Number(rawReplacement) } : { text: rawReplacement };
  const payload = {
    target_kind: targetKind,
    target_id: targetId,
    original: { text: selectedLine?.raw_text || selectedLine?.text || /** @type {HTMLTextAreaElement} */ (byId("correction-original")).value },
    replacement,
    reason: /** @type {HTMLInputElement} */ (byId("correction-reason")).value,
    status: /** @type {HTMLSelectElement} */ (byId("correction-status")).value,
    source_line_ids: selectedLine ? [selectedLine.line_id] : [],
    geometry: selectedLine?.normalized_bbox || null,
  };
  try {
    await api(`/api/v1/history/${workspace.job_id}/corrections`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    form.reset();
    await loadWorkspace();
    setStatus("Correction overlay saved. The original OCR evidence was not changed.");
  } catch (error) { setStatus(error.message, true); }
});

byId("review-source-mode").addEventListener("change", () => {
  const mode = /** @type {HTMLSelectElement} */ (byId("review-source-mode")).value;
  if (mode === "page") { renderPage(); return; }
  /** @type {HTMLSelectElement} */ (byId("review-question")).focus();
});
byId("review-question").addEventListener("change", () => {
  if (!workspace) return;
  const questionId = /** @type {HTMLSelectElement} */ (byId("review-question")).value;
  const question = workspace.questions.find((value) => value.question_id === questionId);
  if (!question) return;
  pageIndex = Math.max(0, workspace.pages.findIndex((page) => page.page === question.source_page));
  renderPage();
  /** @type {HTMLSelectElement} */ (byId("review-source-mode")).value = "question";
  sourceImage.src = `/api/v1/history/${workspace.job_id}/analysis/questions/${encodeURIComponent(questionId)}/crop`;
  sourceImage.alt = `Local source crop for question ${question.display_number}`;
  overlayNode.replaceChildren();
});
byId("review-source-stage").addEventListener("scroll", () => {
  if (synchronizingScroll) return;
  const source = byId("review-source-stage"); const maximum = source.scrollHeight - source.clientHeight;
  if (maximum <= 0) return;
  synchronizingScroll = true; linesNode.scrollTop = (source.scrollTop / maximum) * Math.max(0, linesNode.scrollHeight - linesNode.clientHeight); requestAnimationFrame(() => { synchronizingScroll = false; });
});
linesNode.addEventListener("scroll", () => {
  if (synchronizingScroll) return;
  const maximum = linesNode.scrollHeight - linesNode.clientHeight; if (maximum <= 0) return;
  synchronizingScroll = true; const source = byId("review-source-stage"); source.scrollTop = (linesNode.scrollTop / maximum) * Math.max(0, source.scrollHeight - source.clientHeight); requestAnimationFrame(() => { synchronizingScroll = false; });
});

loadHistory().catch((error) => setStatus(error.message, true));

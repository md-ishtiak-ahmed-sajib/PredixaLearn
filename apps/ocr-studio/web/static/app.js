import { fetchJson } from "./modules/api.js";
import { createProgressRenderer } from "./modules/progress.js";
import { primaryOutput, safeMarkdown } from "./modules/results.js";
import { clearActiveJob, loadActiveJob, saveActiveJob } from "./modules/state.js";
import { createUploadController } from "./modules/upload.js";

"use strict";

const POLL_MIN_MS = 750;
const POLL_MAX_MS = 5000;

const PRIMARY_MODES = {
  document: { workflow: "text" },
  vision_language: { workflow: "vl" },
};
const DOCUMENT_STRATEGIES = {
  complete: {
    workflow: "text",
    help: "Complete document combines OCR, structure, figures, formulas, and verified tables.",
  },
  table_focused: {
    workflow: "table",
    help: "Table-focused extraction keeps the full bundle and emphasizes verified CSV/XLSX.",
  },
  layout_diagnostics: {
    workflow: "layout",
    help: "Layout diagnostics provides specialist geometry and reading-order information.",
  },
};

const state = {
  workflow: "text",
  primaryMode: "document",
  documentIntent: "exam",
  documentStrategy: "complete",
  file: null,
  processing: false,
  engineReady: false,
  maxUploadBytes: Number.MAX_SAFE_INTEGER,
  acceptedExtensions: [],
  languages: [{ code: "en", label: "English" }],
  languageIndex: -1,
  activityKeys: new Set(),
  activeStep: "upload",
  currentJobId: null,
  currentResult: null,
  pollController: null,
  removeTerms: [],
};

/** @type {Record<string, any>} */
const elements = {
  tabs: Array.from(document.querySelectorAll(".workflow-tab")),
  intentTabs: Array.from(document.querySelectorAll("input[name='document-intent']")),
  workflowModeTabs: document.querySelector("#workflow-mode-tabs"),
  advancedSettings: document.querySelector("#advanced-settings"),
  documentOptions: document.querySelector("#document-options"),
  documentStrategy: document.querySelector("#document-strategy"),
  documentStrategyHelp: document.querySelector("#document-strategy-help"),
  dropZone: document.querySelector("#drop-zone"),
  uploadHelp: document.querySelector("#upload-help"),
  uploadError: document.querySelector("#upload-error"),
  fileInput: document.querySelector("#file-input"),
  fileCard: document.querySelector("#file-card"),
  filePreview: document.querySelector("#file-preview"),
  fileName: document.querySelector("#file-name"),
  fileSize: document.querySelector("#file-size"),
  fileMeta: document.querySelector("#file-meta"),
  removeFile: document.querySelector("#remove-file"),
  judgeDemo: document.querySelector("#judge-demo"),
  removeText: document.querySelector("#remove-text"),
  removeTermChips: document.querySelector("#remove-term-chips"),
  language: document.querySelector("#ocr-language"),
  languageSearch: document.querySelector("#language-search"),
  languageList: document.querySelector("#language-list"),
  documentProfile: document.querySelector("#document-profile"),
  runButton: document.querySelector("#run-button"),
  engineNote: document.querySelector("#engine-note"),
  progress: document.querySelector("#progress"),
  progressSpinner: document.querySelector("#progress-spinner"),
  progressText: document.querySelector("#progress-text"),
  cancelJob: document.querySelector("#cancel-job"),
  pipelineSteps: Array.from(document.querySelectorAll(".pipeline [data-step]")),
  pageProgress: document.querySelector("#page-progress"),
  pageSummary: document.querySelector("#page-summary"),
  pageProgressBar: document.querySelector("#page-progress-bar"),
  pageGrid: document.querySelector("#page-grid"),
  activityLog: document.querySelector("#activity-log"),
  resultTitle: document.querySelector("#result-title"),
  emptyResult: document.querySelector("#empty-result"),
  resultView: document.querySelector("#result-view"),
  resultTabs: Array.from(document.querySelectorAll("[data-result-tab]")),
  resultPanels: Array.from(document.querySelectorAll(".result-tab-panel")),
  resultPreview: document.querySelector("#result-preview"),
  resultOutput: document.querySelector("#result-output"),
  resultQuality: document.querySelector("#result-quality"),
  qualityDetails: document.querySelector("#quality-details"),
  warningList: document.querySelector("#warning-list"),
  warningCount: document.querySelector("#warning-count"),
  jobId: document.querySelector("#job-id"),
  resultActions: document.querySelector("#result-actions"),
  copyResult: document.querySelector("#copy-result"),
  revealOutput: document.querySelector("#reveal-output"),
  retryJob: document.querySelector("#retry-job"),
  processAnother: document.querySelector("#process-another"),
  actionStatus: document.querySelector("#action-status"),
  downloadMarkdown: document.querySelector("#download-markdown"),
  downloadDocx: document.querySelector("#download-docx"),
  tableDownloads: document.querySelector("#table-downloads"),
  openAnalysis: document.querySelector("#open-analysis"),
  languageHelp: document.querySelector("#ocr-language-help"),
};

const uploads = createUploadController({ elements, state, updateRunButton });
const { clearFile, formatBytes, setFile, showUploadError } = uploads;
const { renderProgress, resetVisualization } = createProgressRenderer({ elements, state });

function updateRunButton() {
  elements.runButton.disabled = state.processing || !state.file || !state.engineReady;
}

function applyWorkflowSelection() {
  const isVisionLanguage = state.primaryMode === "vision_language";
  const advanced = state.documentIntent === "advanced";
  state.workflow = isVisionLanguage
    ? PRIMARY_MODES.vision_language.workflow
    : DOCUMENT_STRATEGIES[state.documentStrategy]?.workflow || "text";
  elements.tabs.forEach((tab) => {
    const active = tab.dataset.mode === state.primaryMode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  elements.workflowModeTabs.classList.toggle("hidden", !advanced);
  elements.advancedSettings.hidden = !advanced;
  if (advanced) elements.advancedSettings.open = true;
  elements.documentStrategy.value = state.documentStrategy;
  elements.documentStrategy.disabled = state.processing || isVisionLanguage || !advanced;
  elements.documentOptions.classList.toggle("is-disabled", isVisionLanguage || !advanced);
  elements.documentOptions.setAttribute("aria-disabled", String(isVisionLanguage || !advanced));
  elements.documentStrategyHelp.textContent = isVisionLanguage
    ? "Switch to Document OCR to choose a specialized processing strategy."
    : DOCUMENT_STRATEGIES[state.documentStrategy]?.help || DOCUMENT_STRATEGIES.complete.help;
  elements.languageSearch.disabled = true;
  elements.documentProfile.disabled = state.processing || isVisionLanguage || !advanced;
  elements.languageHelp.textContent = "This release scans, extracts, analyzes, and exports English documents only. Additional languages can be implemented later.";
  updateRunButton();
}

function applyDocumentIntent() {
  const advanced = state.documentIntent === "advanced";
  elements.intentTabs.forEach((tab) => {
    const active = tab.value === state.documentIntent;
    tab.checked = active;
    tab.closest(".intent-option")?.classList.toggle("active", active);
  });
  if (!advanced) {
    state.primaryMode = "document";
    state.documentStrategy = "complete";
    elements.documentProfile.value = state.documentIntent === "exam" ? "exam" : "general";
  }
  applyWorkflowSelection();
}

function renderRemoveTerms() {
  elements.removeTermChips.replaceChildren();
  state.removeTerms.forEach((term) => {
    const chip = document.createElement("span");
    chip.className = "remove-term-chip";
    chip.append(document.createTextNode(term));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${term}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      state.removeTerms = state.removeTerms.filter((value) => value !== term);
      renderRemoveTerms();
    });
    chip.append(remove);
    elements.removeTermChips.append(chip);
  });
}

function addRemoveTerms(value) {
  const candidates = value.split(/[,;\r\n]+/).map((term) => term.trim()).filter(Boolean);
  candidates.forEach((term) => {
    if (term.length <= 100 && state.removeTerms.length < 10
        && !state.removeTerms.some((value) => value.toLowerCase() === term.toLowerCase())) {
      state.removeTerms.push(term);
    }
  });
  elements.removeText.value = "";
  renderRemoveTerms();
}

function selectLanguage(language) {
  elements.language.value = language.code;
  elements.languageSearch.value = language.label;
  elements.languageSearch.setAttribute("aria-activedescendant", "");
  elements.languageSearch.setAttribute("aria-expanded", "false");
  elements.languageList.classList.add("hidden");
  state.languageIndex = -1;
}

function renderLanguageOptions(query = "") {
  const normalized = query.trim().toLowerCase();
  const matches = state.languages.filter((language) => (
    !normalized
    || language.label.toLowerCase().includes(normalized)
    || language.code.toLowerCase().includes(normalized)
  )).slice(0, 50);
  elements.languageList.replaceChildren();
  matches.forEach((language, index) => {
    const option = document.createElement("li");
    option.id = `language-option-${index}`;
    option.role = "option";
    option.dataset.code = language.code;
    option.textContent = language.label;
    option.setAttribute("aria-selected", String(language.code === elements.language.value));
    option.addEventListener("mousedown", (event) => {
      event.preventDefault();
      selectLanguage(language);
    });
    elements.languageList.append(option);
  });
  const expanded = matches.length > 0;
  elements.languageList.classList.toggle("hidden", !expanded);
  elements.languageSearch.setAttribute("aria-expanded", String(expanded));
  state.languageIndex = -1;
}

async function loadCapabilities() {
  try {
    const capabilities = await fetchJson("/api/v1/capabilities");
    state.languages = Array.isArray(capabilities.languages) && capabilities.languages.length
      ? capabilities.languages
      : state.languages;
    state.maxUploadBytes = Number(capabilities.limits?.max_upload_bytes)
      || state.maxUploadBytes;
    state.acceptedExtensions = capabilities.limits?.file_extensions || [];
    elements.uploadHelp.textContent = `PDF, PNG, JPEG, BMP, TIFF, or WebP · up to ${formatBytes(state.maxUploadBytes)}`;
    elements.language.replaceChildren();
    state.languages.forEach((language) => {
      const option = document.createElement("option");
      option.value = language.code;
      option.textContent = language.label;
      option.selected = language.code === capabilities.defaults?.language;
      elements.language.append(option);
    });
    const selected = state.languages.find((item) => item.code === elements.language.value)
      || state.languages[0];
    selectLanguage(selected);
    if (Array.isArray(capabilities.document_strategies)) {
      elements.documentStrategy.replaceChildren();
      capabilities.document_strategies.forEach((strategy) => {
        if (!DOCUMENT_STRATEGIES[strategy.id]) return;
        const option = document.createElement("option");
        option.value = strategy.id;
        option.textContent = strategy.recommended
          ? `${strategy.label} (recommended)` : strategy.label;
        elements.documentStrategy.append(option);
      });
    }
    if (DOCUMENT_STRATEGIES[capabilities.defaults?.document_strategy]) {
      state.documentStrategy = capabilities.defaults.document_strategy;
    }
  } catch {
    showUploadError("Could not load service limits. Server validation will still protect the upload.");
  }
  applyWorkflowSelection();
}

function qualityFacts(value) {
  return [
    ["Pages / images", value?.page_count],
    ["OCR language", value?.language === "en" ? "English" : "Legacy language record"],
    ["Document profile", value?.document_profile],
    ["Export coverage", value?.content_coverage?.coverage_percent],
    ["Unaccounted lines", value?.content_coverage?.unaccounted_line_count],
    ["Recognition quality", value?.recognition_quality?.status],
    ["Artifact export", value?.artifact_status],
    ["DOCX generation", value?.docx_generation?.status],
    ["DOCX render validation", value?.document_validation?.status],
    ["Tables", value?.table_count],
    ["Automatic corrections", value?.correction_count],
    ["Review suggestions", value?.review_count],
  ].filter(([, fact]) => Number.isFinite(fact) || typeof fact === "string");
}

function renderQuality(value) {
  elements.resultQuality.replaceChildren();
  qualityFacts(value).forEach(([label, fact]) => {
    const group = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = String(fact);
    group.append(term, description);
    elements.resultQuality.append(group);
  });
  elements.qualityDetails.replaceChildren();
  const groups = [
    ["Review suggestions", value?.review_suggestions],
    ["Applied removals", value?.removed_terms],
    ["Automatic corrections", value?.corrections],
  ];
  groups.forEach(([title, items]) => {
    if (!Array.isArray(items) || !items.length) return;
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `${title} (${items.length})`;
    const list = document.createElement("ul");
    items.forEach((item) => {
      const entry = document.createElement("li");
      entry.textContent = typeof item === "string" ? item : JSON.stringify(item);
      list.append(entry);
    });
    details.append(summary, list);
    elements.qualityDetails.append(details);
  });
}

function activateResultTab(name, focus = false) {
  elements.resultTabs.forEach((tab) => {
    const active = tab.dataset.resultTab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    if (active && focus) tab.focus();
  });
  elements.resultPanels.forEach((panel) => {
    const active = panel.id === ({
      preview: "result-preview",
      quality: "result-quality-panel",
      warnings: "result-warnings",
      raw: "result-output",
    })[name];
    panel.classList.toggle("hidden", !active);
  });
}

function showResult(title, value) {
  state.currentResult = value;
  elements.resultTitle.textContent = title;
  elements.emptyResult.classList.add("hidden");
  elements.resultView.classList.remove("hidden");
  const primary = primaryOutput(value, state.workflow);
  elements.resultPreview.replaceChildren();
  if (primary.format === "markdown") {
    const fragment = safeMarkdown(primary.text);
    if (fragment) elements.resultPreview.append(fragment);
    else elements.resultPreview.textContent = primary.text;
  } else {
    const pre = document.createElement("pre");
    pre.textContent = primary.text;
    elements.resultPreview.append(pre);
  }
  elements.resultOutput.textContent = JSON.stringify(value, null, 2);
  renderQuality(value);
  const warnings = [
    ...(Array.isArray(value?.warnings) ? value.warnings : []),
    ...(Array.isArray(value?.review_suggestions) ? value.review_suggestions : []),
  ];
  elements.warningList.replaceChildren();
  if (!warnings.length) {
    const item = document.createElement("li");
    item.textContent = "No warnings were reported.";
    elements.warningList.append(item);
  } else {
    warnings.forEach((warning) => {
      const item = document.createElement("li");
      item.textContent = typeof warning === "string" ? warning : JSON.stringify(warning);
      elements.warningList.append(item);
    });
  }
  elements.warningCount.textContent = warnings.length ? String(warnings.length) : "";
  activateResultTab("preview");

  const markdownAvailable = Boolean(value?.artifacts?.markdown && state.currentJobId);
  const docxAvailable = Boolean(value?.artifacts?.docx && state.currentJobId);
  const downloadableTables = Array.isArray(value?.tables)
    ? value.tables.map((table, index) => ({ table, index }))
      .filter(({ table }) => table?.status === "accepted")
    : [];
  elements.resultActions.classList.remove("hidden");
  const canAnalyze = Boolean(state.currentJobId && value?.document_profile === "exam");
  elements.openAnalysis.classList.toggle("hidden", !canAnalyze);
  if (canAnalyze) elements.openAnalysis.href = `/analyze?job=${encodeURIComponent(state.currentJobId)}`;
  elements.downloadMarkdown.classList.toggle("hidden", !markdownAvailable);
  elements.downloadDocx.classList.toggle("hidden", !docxAvailable);
  elements.revealOutput.classList.toggle("hidden", !state.currentJobId);
  elements.tableDownloads.replaceChildren();
  if (state.currentJobId) {
    const job = encodeURIComponent(state.currentJobId);
    elements.downloadMarkdown.href = `/api/v1/history/${job}/download?format=markdown`;
    elements.downloadDocx.href = `/api/v1/history/${job}/download?format=docx`;
    downloadableTables.forEach(({ table, index }) => {
      const tableNumber = Number.isInteger(table.table_index) ? table.table_index + 1 : index + 1;
      ["csv", "xlsx"].forEach((format) => {
        const link = document.createElement("a");
        link.className = "quiet-button";
        link.href = `/api/v1/history/${job}/download?format=${format}&table=${tableNumber}`;
        link.textContent = `Table ${tableNumber} ${format.toUpperCase()}`;
        elements.tableDownloads.append(link);
      });
    });
  }
}

function setProcessing(processing) {
  state.processing = processing;
  elements.removeText.disabled = processing;
  elements.removeFile.disabled = processing;
  elements.judgeDemo.disabled = processing || !state.engineReady;
  elements.tabs.forEach((tab) => { tab.disabled = processing; });
  elements.intentTabs.forEach((tab) => { tab.disabled = processing; });
  applyWorkflowSelection();
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitUntilVisible() {
  if (!document.hidden) return;
  await new Promise((resolve) => {
    const listener = () => {
      if (!document.hidden) {
        document.removeEventListener("visibilitychange", listener);
        resolve(undefined);
      }
    };
    document.addEventListener("visibilitychange", listener);
  });
}

function rememberActiveJob(jobId) {
  saveActiveJob({
    job_id: jobId,
    input_name: state.file?.name || "Document",
    workflow: state.workflow,
  });
}

async function pollJob(jobId) {
  let delay = POLL_MIN_MS;
  let lastSignature = "";
  let failedSince = null;
  while (true) {
    await waitUntilVisible();
    state.pollController = new AbortController();
    try {
      const job = await fetchJson(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
        signal: state.pollController.signal,
      });
      failedSince = null;
      const signature = JSON.stringify([job.status, job.progress]);
      delay = signature === lastSignature ? Math.min(POLL_MAX_MS, Math.round(delay * 1.5)) : POLL_MIN_MS;
      lastSignature = signature;
      if (job.progress) {
        renderProgress({ ...job.progress, cancel_requested: job.cancel_requested }, job.status);
      }
      if (job.status === "completed") return job;
      if (job.status === "failed" || job.status === "cancelled") return job;
    } catch (error) {
      if (error.name === "AbortError") continue;
      if (error.status === 404) throw error;
      failedSince ||= Date.now();
      if (Date.now() - failedSince > 30_000) throw error;
      delay = Math.min(POLL_MAX_MS, Math.round(delay * 1.6));
      elements.progressText.textContent = "Connection interrupted; retrying locally…";
    }
    await wait(delay);
  }
}

async function finishPolling(jobId) {
  const terminal = await pollJob(jobId);
  clearActiveJob();
  if (terminal.status === "completed") {
    showResult("Processing complete", terminal.result);
    return;
  }
  const message = terminal.error || (terminal.status === "cancelled"
    ? "Processing was cancelled." : "The OCR job failed.");
  renderProgress({ ...terminal.progress, message }, terminal.status);
  showResult(terminal.status === "cancelled" ? "Processing cancelled" : "Processing failed", {
    error: message,
  });
}

async function runJudgeDemo() {
  if (state.processing || !state.engineReady) return;
  setProcessing(true);
  resetVisualization();
  renderProgress({ stage: "queued", message: "Preparing the original synthetic Judge Demo", completed_pages: 0 }, "pending");
  elements.resultTitle.textContent = "Running the Judge Demo";
  elements.emptyResult.classList.remove("hidden");
  elements.jobId.textContent = "";
  state.currentJobId = null;
  try {
    const submission = await fetchJson("/api/v1/demo/judge", { method: "POST" });
    state.currentJobId = submission.job_id;
    state.workflow = "text";
    state.documentIntent = "exam";
    elements.documentProfile.value = "exam";
    elements.jobId.textContent = submission.job_id.slice(0, 12);
    saveActiveJob({ job_id: submission.job_id, input_name: "PredixaLearn Judge Demo", workflow: "text" });
    await finishPolling(submission.job_id);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Judge Demo could not start.";
    renderProgress({ stage: "failed", message, completed_pages: 0 }, "failed");
    showResult("Judge Demo failed", { error: message });
  } finally {
    setProcessing(false);
  }
}

async function runWorkflow() {
  if (!state.file || state.processing || !state.engineReady) return;
  setProcessing(true);
  resetVisualization();
  renderProgress({ stage: "uploading", message: "Uploading and validating the document", completed_pages: 0 });
  elements.resultTitle.textContent = "Working on your document";
  elements.emptyResult.classList.remove("hidden");
  elements.jobId.textContent = "";
  state.currentJobId = null;
  const formData = new FormData();
  formData.append("file", state.file, state.file.name);
  if (elements.removeText.value.trim()) addRemoveTerms(elements.removeText.value);
  state.removeTerms.forEach((term) => formData.append("remove_terms", term));
  if (state.primaryMode !== "vision_language") {
    formData.append("language", elements.language.value || "en");
    formData.append("document_profile", elements.documentProfile.value || "auto");
  }
  try {
    const submission = await fetchJson(`/api/v1/ocr/${state.workflow}?async_mode=true`, {
      method: "POST",
      body: formData,
    });
    state.currentJobId = submission.job_id;
    elements.jobId.textContent = submission.job_id.slice(0, 12);
    rememberActiveJob(submission.job_id);
    renderProgress({ stage: "queued", message: "Upload verified; waiting for the OCR engine", completed_pages: 0 }, "pending");
    await finishPolling(submission.job_id);
  } catch (error) {
    clearActiveJob();
    const message = error instanceof Error ? error.message : "Unknown error";
    renderProgress({ stage: "failed", message, completed_pages: elements.pageProgressBar.value }, "failed");
    showResult("Processing failed", { error: message });
  } finally {
    setProcessing(false);
  }
}

async function resumeActiveJob() {
  const saved = loadActiveJob();
  if (!saved?.job_id) return;
  state.currentJobId = saved.job_id;
  elements.jobId.textContent = saved.job_id.slice(0, 12);
  elements.resultTitle.textContent = `Resuming ${saved.input_name || "document"}`;
  setProcessing(true);
  resetVisualization();
  try {
    await finishPolling(saved.job_id);
  } catch (error) {
    clearActiveJob();
    const message = error instanceof Error ? error.message : "The saved job could not be resumed.";
    showResult("Could not resume processing", { error: message });
  } finally {
    setProcessing(false);
  }
}

elements.tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => {
    state.primaryMode = tab.dataset.mode === "vision_language" ? "vision_language" : "document";
    applyWorkflowSelection();
  });
  tab.addEventListener("keydown", (event) => {
    let targetIndex = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") targetIndex = (index + 1) % elements.tabs.length;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") targetIndex = (index - 1 + elements.tabs.length) % elements.tabs.length;
    if (event.key === "Home") targetIndex = 0;
    if (event.key === "End") targetIndex = elements.tabs.length - 1;
    if (targetIndex === null) return;
    event.preventDefault();
    elements.tabs[targetIndex].focus();
    elements.tabs[targetIndex].click();
  });
});

elements.intentTabs.forEach((tab) => {
  tab.addEventListener("change", () => {
    if (!tab.checked) return;
    state.documentIntent = tab.value || "exam";
    applyDocumentIntent();
  });
});

elements.resultTabs.forEach((tab, index) => {
  tab.addEventListener("click", () => activateResultTab(tab.dataset.resultTab));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    let target = event.key === "Home" ? 0 : event.key === "End" ? elements.resultTabs.length - 1
      : event.key === "ArrowRight" ? (index + 1) % elements.resultTabs.length
        : (index - 1 + elements.resultTabs.length) % elements.resultTabs.length;
    activateResultTab(elements.resultTabs[target].dataset.resultTab, true);
  });
});

elements.documentStrategy.addEventListener("change", () => {
  if (DOCUMENT_STRATEGIES[elements.documentStrategy.value]) {
    state.documentStrategy = elements.documentStrategy.value;
    state.primaryMode = "document";
    state.documentIntent = "advanced";
    applyDocumentIntent();
  }
});
elements.languageSearch.addEventListener("focus", () => renderLanguageOptions(elements.languageSearch.value));
elements.languageSearch.addEventListener("input", () => renderLanguageOptions(elements.languageSearch.value));
elements.languageSearch.addEventListener("blur", () => {
  window.setTimeout(() => {
    const selected = state.languages.find((item) => item.code === elements.language.value);
    if (selected) selectLanguage(selected);
  }, 100);
});
elements.languageSearch.addEventListener("keydown", (event) => {
  const options = Array.from(elements.languageList.querySelectorAll("[role=option]"));
  if (event.key === "Escape") {
    elements.languageList.classList.add("hidden");
    elements.languageSearch.setAttribute("aria-expanded", "false");
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    state.languageIndex = event.key === "ArrowDown"
      ? Math.min(options.length - 1, state.languageIndex + 1)
      : Math.max(0, state.languageIndex - 1);
    options.forEach((option, index) => option.classList.toggle("active", index === state.languageIndex));
    if (options[state.languageIndex]) {
      elements.languageSearch.setAttribute("aria-activedescendant", options[state.languageIndex].id);
      options[state.languageIndex].scrollIntoView({ block: "nearest" });
    }
  }
  if (event.key === "Enter" && options[state.languageIndex]) {
    event.preventDefault();
    const language = state.languages.find((item) => item.code === options[state.languageIndex].dataset.code);
    if (language) selectLanguage(language);
  }
});
elements.dropZone.addEventListener("click", () => elements.fileInput.click());
elements.dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    elements.fileInput.click();
  }
});
elements.fileInput.addEventListener("change", () => setFile(elements.fileInput.files[0]));
elements.removeFile.addEventListener("click", clearFile);
elements.judgeDemo.addEventListener("click", runJudgeDemo);
elements.removeText.addEventListener("keydown", (event) => {
  if (["Enter", ",", ";"].includes(event.key)) {
    event.preventDefault();
    addRemoveTerms(elements.removeText.value);
  }
});
elements.removeText.addEventListener("blur", () => {
  if (elements.removeText.value.trim()) addRemoveTerms(elements.removeText.value);
});
elements.runButton.addEventListener("click", runWorkflow);
elements.cancelJob.addEventListener("click", async () => {
  if (!state.currentJobId) return;
  elements.cancelJob.disabled = true;
  elements.cancelJob.textContent = "Cancelling…";
  try {
    const job = await fetchJson(`/api/v1/jobs/${encodeURIComponent(state.currentJobId)}`, { method: "DELETE" });
    if (job.progress) renderProgress({ ...job.progress, cancel_requested: true }, job.status);
  } catch (error) {
    elements.cancelJob.disabled = false;
    elements.cancelJob.textContent = "Cancel";
    elements.progressText.textContent = error instanceof Error ? error.message : "Cancellation failed.";
  }
});
elements.copyResult.addEventListener("click", async () => {
  if (!state.currentResult) return;
  try {
    await navigator.clipboard.writeText(primaryOutput(state.currentResult, state.workflow).text);
    elements.actionStatus.textContent = "Output copied to the clipboard.";
    elements.copyResult.textContent = "Copied";
    window.setTimeout(() => { elements.copyResult.textContent = "Copy output"; }, 1200);
  } catch {
    elements.actionStatus.textContent = "Output could not be copied.";
  }
});
elements.revealOutput.addEventListener("click", async () => {
  if (!state.currentJobId) return;
  try {
    await fetchJson(`/api/v1/history/${encodeURIComponent(state.currentJobId)}/reveal`, { method: "POST" });
    elements.actionStatus.textContent = "Output folder opened.";
  } catch (error) {
    elements.actionStatus.textContent = error instanceof Error ? error.message : "Output folder could not be opened.";
  }
});
elements.retryJob.addEventListener("click", () => {
  if (state.file) runWorkflow();
  else {
    showUploadError("Choose the source document again before retrying.");
    elements.dropZone.focus();
  }
});
elements.processAnother.addEventListener("click", () => {
  clearFile();
  resetVisualization();
  state.currentResult = null;
  state.currentJobId = null;
  elements.jobId.textContent = "";
  elements.resultTitle.textContent = "Ready when you are";
  elements.openAnalysis.classList.add("hidden");
  elements.emptyResult.classList.remove("hidden");
  elements.dropZone.focus();
});
["dragenter", "dragover"].forEach((name) => elements.dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  elements.dropZone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => elements.dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  elements.dropZone.classList.remove("dragging");
}));
elements.dropZone.addEventListener("drop", (event) => setFile(event.dataTransfer.files[0]));
window.addEventListener("predixalearn:health", (event) => {
  const health = event.detail;
  state.engineReady = health?.warmup?.status === "ready" || health?.status === "ready";
  elements.engineNote.textContent = state.engineReady
    ? "OCR engine ready. Processing stays on this computer."
    : health?.warmup?.status === "failed"
      ? "OCR engine initialization failed. Open Tips for diagnostics."
      : "Preparing the OCR engine… You can choose a file while it starts.";
  elements.engineNote.classList.toggle("error-callout", health?.warmup?.status === "failed");
  updateRunButton();
});
window.addEventListener("beforeunload", () => {
  uploads.dispose();
  state.pollController?.abort();
});

applyDocumentIntent();
loadCapabilities();
if (window.predixalearnHealth) {
  window.dispatchEvent(new CustomEvent("predixalearn:health", { detail: window.predixalearnHealth }));
}
resumeActiveJob();

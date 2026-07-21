import { historyFetch } from "./modules/history-api.js";
import { formatHistoryDate, formatHistoryDuration } from "./modules/history-format.js";

const HISTORY_PAGE_SIZE = 12;
const RERUN_KEY = "predixalearn.rerun.v1";
const ACTIVE_STATUSES = new Set(["pending", "processing"]);
const WORKFLOW_LABELS = {
  text_recognition: "Document OCR",
  layout_parsing: "Layout diagnostics",
  table_extraction: "Table-focused",
  vl_processing: "Vision-Language",
};

const historyState = { offset: 0, total: 0, active: false, pollTimer: null, selected: null, checked: new Set() };
/** @type {Record<string, any>} */
const el = {
  search: document.querySelector("#history-search"), workflow: document.querySelector("#workflow-filter"),
  status: document.querySelector("#status-filter"), pinned: document.querySelector("#pinned-filter"),
  createdFrom: document.querySelector("#created-from"), createdTo: document.querySelector("#created-to"),
  sort: document.querySelector("#sort-filter"), order: document.querySelector("#order-filter"),
  apply: document.querySelector("#apply-filters"), clear: document.querySelector("#clear-history"),
  count: document.querySelector("#history-count"), error: document.querySelector("#history-error"),
  list: document.querySelector("#history-list"), empty: document.querySelector("#history-empty"),
  previous: document.querySelector("#previous-page"), next: document.querySelector("#next-page"),
  pageLabel: document.querySelector("#page-label"), bulk: document.querySelector("#bulk-actions"),
  selectedCount: document.querySelector("#selected-count"), exportSelected: document.querySelector("#export-selected"),
  deleteSelected: document.querySelector("#delete-selected"), clearSelection: document.querySelector("#clear-selection"),
  importButton: document.querySelector("#import-history"), importFile: document.querySelector("#import-file"),
  dialog: document.querySelector("#history-detail"), closeDetail: document.querySelector("#close-detail"),
  detailTitle: document.querySelector("#detail-title"), detailMeta: document.querySelector("#detail-meta"),
  detailQuality: document.querySelector("#detail-quality"), detailQualityDetails: document.querySelector("#detail-quality-details"),
  primaryOutput: document.querySelector("#primary-output"), rawOutput: document.querySelector("#raw-output"),
  copyResult: document.querySelector("#copy-result"), pinResult: document.querySelector("#pin-result"),
  rerunResult: document.querySelector("#rerun-result"), revealResult: document.querySelector("#reveal-result"),
  downloadPrimary: document.querySelector("#download-primary"), downloadMarkdown: document.querySelector("#download-markdown"),
  downloadDocx: document.querySelector("#download-docx"), downloadJson: document.querySelector("#download-json"),
  tableDownloads: document.querySelector("#detail-table-downloads"), legacyExplanation: document.querySelector("#legacy-explanation"),
  confirm: document.querySelector("#confirm-dialog"), confirmTitle: document.querySelector("#confirm-title"),
  confirmMessage: document.querySelector("#confirm-message"), confirmCancel: document.querySelector("#confirm-cancel"),
  confirmAccept: document.querySelector("#confirm-accept"),
  institutionStatus: document.querySelector("#institution-sync-status"),
  institutionForm: document.querySelector("#institution-enroll-form"),
  institutionUrl: document.querySelector("#institution-url"),
  institutionToken: document.querySelector("#institution-token"),
  includeInstitutionPreviews: document.querySelector("#include-institution-previews"),
  queueInstitution: document.querySelector("#queue-institution"),
  syncInstitution: document.querySelector("#sync-institution-now"),
};

function makeText(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value;
  return node;
}
function showError(message = "") {
  el.error.textContent = message;
  el.error.classList.toggle("hidden", !message);
}

function updateBulkActions() {
  const count = historyState.checked.size;
  el.bulk.classList.toggle("hidden", count === 0);
  el.selectedCount.textContent = `${count} selected`;
}

function confirmAction(title, message, acceptLabel = "Confirm") {
  el.confirmTitle.textContent = title;
  el.confirmMessage.textContent = message;
  el.confirmAccept.textContent = acceptLabel;
  el.confirm.showModal();
  return new Promise((resolve) => {
    const finish = (accepted) => {
      el.confirmAccept.removeEventListener("click", accept);
      el.confirmCancel.removeEventListener("click", cancel);
      el.confirm.removeEventListener("cancel", cancel);
      if (el.confirm.open) el.confirm.close();
      resolve(accepted);
    };
    const accept = () => finish(true);
    const cancel = (event) => { event?.preventDefault(); finish(false); };
    el.confirmAccept.addEventListener("click", accept);
    el.confirmCancel.addEventListener("click", cancel);
    el.confirm.addEventListener("cancel", cancel);
  });
}

function historyFact(label, value) {
  const group = document.createElement("div");
  group.append(makeText("dt", "", label), makeText("dd", "", value));
  return group;
}

function createHistoryCard(item) {
  const card = document.createElement("article");
  card.className = "history-card";
  card.dataset.jobId = item.job_id;
  if (item.pinned) card.classList.add("pinned");
  const heading = document.createElement("div");
  heading.className = "history-card-heading";
  const selection = document.createElement("label");
  selection.className = "history-select";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = historyState.checked.has(item.job_id);
  checkbox.setAttribute("aria-label", `Select ${item.input_name}`);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) historyState.checked.add(item.job_id); else historyState.checked.delete(item.job_id);
    updateBulkActions();
  });
  selection.append(checkbox);
  const titleGroup = document.createElement("div");
  titleGroup.append(makeText("h2", "", item.input_name), makeText("p", "history-time", `Submitted ${formatHistoryDate(item.created_at)}`));
  const status = makeText("span", `status-pill status-${item.status}`, item.status);
  heading.append(selection, titleGroup, status);
  const facts = document.createElement("dl");
  facts.className = "history-facts";
  facts.append(
    historyFact("Workflow", WORKFLOW_LABELS[item.workflow] || item.workflow),
    historyFact("Pages / images", Number.isInteger(item.item_count) ? String(item.item_count) : "—"),
    historyFact("Duration", formatHistoryDuration(item.duration_ms)),
    historyFact("Quality", Number.isFinite(item.quality_score) ? `${item.quality_score}%` : "—"),
    historyFact("Warnings", String(item.warning_count || 0)),
  );
  const actions = document.createElement("div");
  actions.className = "history-actions";
  const actionDefinitions = [
    ["view", "View result", "secondary-button"],
    ["analyze", "Analyze", "quiet-button"],
    ["rerun", "Run again", "quiet-button"],
    ["pin", item.pinned ? "Unpin" : "Pin", "quiet-button"],
    ["reveal", "Reveal", "quiet-button"],
    ["delete", "Delete", "quiet-button delete-action"],
  ];
  actionDefinitions.forEach(([action, label, className]) => {
    const button = makeText("button", className, label);
    button.type = "button";
    button.dataset.action = action;
    if (action === "reveal" && !item.artifacts_available) button.disabled = true;
    if (action === "analyze" && item.status !== "completed") button.disabled = true;
    actions.append(button);
  });
  card.append(heading, facts);
  if (item.error) card.append(makeText("p", "history-error-message", item.error));
  if (item.legacy) card.append(makeText("p", "history-legacy-message", "Legacy result — re-upload for a complete modern export."));
  card.append(actions);
  return card;
}

function queryParameters() {
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE), offset: String(historyState.offset) });
  if (el.search.value.trim()) params.set("q", el.search.value.trim());
  if (el.workflow.value) params.set("workflow", el.workflow.value);
  if (el.status.value) params.set("status", el.status.value);
  if (el.pinned.value) params.set("pinned", el.pinned.value);
  if (el.createdFrom.value) params.set("created_from", `${el.createdFrom.value}T00:00:00Z`);
  if (el.createdTo.value) params.set("created_to", `${el.createdTo.value}T23:59:59.999Z`);
  params.set("sort", el.sort.value);
  params.set("order", el.order.value);
  return params;
}

async function loadHistory() {
  if (historyState.pollTimer !== null) window.clearTimeout(historyState.pollTimer);
  historyState.pollTimer = null;
  showError();
  try {
    const payload = await historyFetch(`/api/v1/history?${queryParameters()}`);
    historyState.total = payload.total;
    historyState.active = payload.items.some((item) => ACTIVE_STATUSES.has(item.status));
    el.list.replaceChildren();
    payload.items.forEach((item) => el.list.append(createHistoryCard(item)));
    el.empty.classList.toggle("hidden", payload.items.length !== 0);
    el.list.classList.toggle("hidden", payload.items.length === 0);
    el.count.textContent = `${payload.total} saved ${payload.total === 1 ? "scan" : "scans"}`;
    const currentPage = Math.floor(historyState.offset / HISTORY_PAGE_SIZE) + 1;
    const totalPages = Math.max(1, Math.ceil(payload.total / HISTORY_PAGE_SIZE));
    el.pageLabel.textContent = `Page ${currentPage} of ${totalPages}`;
    el.previous.disabled = historyState.offset === 0;
    el.next.disabled = historyState.offset + HISTORY_PAGE_SIZE >= payload.total;
    if (historyState.active) historyState.pollTimer = window.setTimeout(loadHistory, 2000);
  } catch (error) {
    showError(error instanceof Error ? error.message : "History failed to load.");
  }
}

async function loadInstitutionStatus() {
  try {
    const status = await historyFetch("/api/v1/institution/status");
    if (!status.enrolled) {
      el.institutionStatus.textContent = "Not enrolled. Local OCR and History remain fully available.";
      el.syncInstitution.disabled = true;
      return;
    }
    const pending = status.outbox.pending + status.outbox.in_flight;
    el.institutionStatus.textContent = `${status.connection.tenant_id} · ${pending} queued · automatic History upload is off`;
    el.syncInstitution.disabled = pending === 0;
  } catch (error) {
    el.institutionStatus.textContent = error instanceof Error ? error.message : "Institution status is unavailable.";
    el.syncInstitution.disabled = true;
  }
}

function renderDetailQuality(detail) {
  const language = detail.language === "en"
    ? "English"
    : `Legacy language record (${detail.language})`;
  const facts = [
    ["Quality", detail.quality_score], ["Warnings", detail.warning_count],
    ["Pages / images", detail.item_count], ["Language", language],
    ["Profile", detail.document_profile], ["Duration", formatHistoryDuration(detail.duration_ms)],
  ].filter(([, value]) => Number.isFinite(value) || (typeof value === "string" && value));
  el.detailQuality.replaceChildren();
  facts.forEach(([label, value]) => el.detailQuality.append(historyFact(label, String(value))));
  el.detailQuality.classList.toggle("hidden", facts.length === 0);
  el.detailQualityDetails.replaceChildren();
  const groups = [["Warnings", detail.result?.warnings], ["Review suggestions", detail.result?.review_suggestions], ["Applied removals", detail.remove_terms]];
  groups.forEach(([title, items]) => {
    if (!Array.isArray(items) || !items.length) return;
    const details = document.createElement("details");
    const list = document.createElement("ul");
    items.forEach((item) => list.append(makeText("li", "", typeof item === "string" ? item : JSON.stringify(item))));
    details.append(makeText("summary", "", `${title} (${items.length})`), list);
    el.detailQualityDetails.append(details);
  });
  el.detailQualityDetails.classList.toggle("hidden", !el.detailQualityDetails.children.length);
}

async function openDetail(jobId) {
  const detail = await historyFetch(`/api/v1/history/${encodeURIComponent(jobId)}`);
  historyState.selected = detail;
  el.detailTitle.textContent = detail.input_name;
  el.detailMeta.textContent = `${WORKFLOW_LABELS[detail.workflow] || detail.workflow} · ${formatHistoryDuration(detail.duration_ms)} · ${formatHistoryDate(detail.created_at)}`;
  el.primaryOutput.textContent = detail.primary_output || "No readable output is available.";
  el.rawOutput.textContent = JSON.stringify(detail.result, null, 2);
  renderDetailQuality(detail);
  el.pinResult.textContent = detail.pinned ? "Unpin" : "Pin";
  const job = encodeURIComponent(jobId);
  el.downloadPrimary.href = `/api/v1/history/${job}/download?format=primary`;
  el.downloadJson.href = `/api/v1/history/${job}/download?format=json`;
  el.downloadMarkdown.href = `/api/v1/history/${job}/download?format=markdown`;
  el.downloadDocx.href = `/api/v1/history/${job}/download?format=docx`;
  el.downloadPrimary.classList.toggle("hidden", !detail.result_available);
  el.downloadJson.classList.toggle("hidden", !detail.result_available);
  el.downloadMarkdown.classList.toggle("hidden", !detail.artifact_availability?.markdown);
  el.downloadDocx.classList.toggle("hidden", !detail.artifact_availability?.docx);
  el.revealResult.disabled = !detail.artifacts_available;
  el.tableDownloads.replaceChildren();
  (detail.table_downloads || []).forEach((table) => {
    ["csv", "xlsx"].forEach((format) => {
      if (!table[format]) return;
      const link = makeText("a", "quiet-button", `Table ${table.table_index} ${format.toUpperCase()}`);
      link.href = `/api/v1/history/${job}/download?format=${format}&table=${table.table_index}`;
      el.tableDownloads.append(link);
    });
  });
  el.tableDownloads.classList.toggle("hidden", !el.tableDownloads.children.length);
  el.legacyExplanation.classList.toggle("hidden", !detail.requires_reupload);
  el.legacyExplanation.textContent = detail.requires_reupload
    ? "This record does not retain its source. Re-upload it to create a complete current export."
    : "";
  el.dialog.showModal();
}

function rememberRerun(detail) {
  localStorage.setItem(RERUN_KEY, JSON.stringify({
    job_id: detail.job_id, workflow: detail.workflow, language: detail.language,
    document_profile: detail.document_profile, remove_terms: detail.remove_terms || [],
  }));
  window.location.href = `/?rerun=${encodeURIComponent(detail.job_id)}`;
}
async function setPinned(jobId, pinned) {
  await historyFetch(`/api/v1/history/${encodeURIComponent(jobId)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pinned }),
  });
  await loadHistory();
}
async function reveal(jobId) {
  await historyFetch(`/api/v1/history/${encodeURIComponent(jobId)}/reveal`, { method: "POST" });
}
async function deleteRecord(jobId, filename) {
  const accepted = await confirmAction("Delete History record?", `Delete ${filename} and its archived result bundle? The latest named export remains.`, "Delete");
  if (!accepted) return;
  await historyFetch(`/api/v1/history/${encodeURIComponent(jobId)}`, { method: "DELETE" });
  historyState.checked.delete(jobId);
  if (el.dialog.open && historyState.selected?.job_id === jobId) el.dialog.close();
  if (historyState.offset >= historyState.total - 1 && historyState.offset > 0) historyState.offset = Math.max(0, historyState.offset - HISTORY_PAGE_SIZE);
  updateBulkActions();
  await loadHistory();
}

el.list.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  const card = event.target.closest("[data-job-id]");
  if (!button || !card) return;
  try {
    const jobId = card.dataset.jobId;
    if (button.dataset.action === "view") await openDetail(jobId);
    if (button.dataset.action === "analyze") window.location.href = `/analyze?job=${encodeURIComponent(jobId)}`;
    if (button.dataset.action === "delete") await deleteRecord(jobId, card.querySelector("h2").textContent);
    if (button.dataset.action === "reveal") await reveal(jobId);
    if (button.dataset.action === "pin") await setPinned(jobId, button.textContent === "Pin");
    if (button.dataset.action === "rerun") rememberRerun(await historyFetch(`/api/v1/history/${encodeURIComponent(jobId)}`));
  } catch (error) { showError(error instanceof Error ? error.message : "History action failed."); }
});
el.apply.addEventListener("click", () => { historyState.offset = 0; loadHistory(); });
el.search.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); historyState.offset = 0; loadHistory(); } });
el.previous.addEventListener("click", () => { historyState.offset = Math.max(0, historyState.offset - HISTORY_PAGE_SIZE); loadHistory(); });
el.next.addEventListener("click", () => { historyState.offset += HISTORY_PAGE_SIZE; loadHistory(); });
el.clearSelection.addEventListener("click", () => { historyState.checked.clear(); updateBulkActions(); loadHistory(); });
el.deleteSelected.addEventListener("click", async () => {
  const ids = Array.from(historyState.checked);
  if (!ids.length || !await confirmAction("Delete selected records?", `Delete ${ids.length} archived result bundles?`, "Delete selected")) return;
  try {
    await historyFetch("/api/v1/history/bulk-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_ids: ids }) });
    historyState.checked.clear(); updateBulkActions(); await loadHistory();
  } catch (error) { showError(error instanceof Error ? error.message : "Selected records could not be deleted."); }
});
el.exportSelected.addEventListener("click", async () => {
  const ids = Array.from(historyState.checked);
  if (!ids.length) return;
  try {
    const response = await fetch("/api/v1/history/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_ids: ids }) });
    if (!response.ok) { const payload = await response.json(); throw new Error(payload.detail || "Export failed."); }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = "predixalearn-history.zip"; link.click();
    URL.revokeObjectURL(url);
  } catch (error) { showError(error instanceof Error ? error.message : "History export failed."); }
});
el.institutionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.institutionStatus.textContent = "Enrolling this device without uploading History…";
  try {
    const result = await historyFetch("/api/v1/institution/enroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: el.institutionUrl.value, enrollment_token: el.institutionToken.value }),
    });
    el.institutionToken.value = "";
    el.institutionStatus.textContent = `${result.tenant_id} enrolled · existing History was not uploaded`;
    await loadInstitutionStatus();
  } catch (error) {
    el.institutionStatus.textContent = error instanceof Error ? error.message : "Institution enrollment failed.";
  }
});
el.queueInstitution.addEventListener("click", async () => {
  const ids = Array.from(historyState.checked);
  if (!ids.length) return;
  const accepted = await confirmAction(
    "Queue selected evidence?",
    `Prepare ${ids.length} selected OCR result(s) for the enrolled institution? Source uploads are not included.${el.includeInstitutionPreviews.checked ? " Up to five watermarked page previews will be explicitly approved and included." : " Page previews will stay local."}`,
    "Queue selected",
  );
  if (!accepted) return;
  try {
    const result = await historyFetch("/api/v1/institution/outbox", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_ids: ids,
        include_approved_previews: el.includeInstitutionPreviews.checked,
      }),
    });
    el.institutionStatus.textContent = `${result.items.length} explicitly selected record(s) queued`;
    await loadInstitutionStatus();
  } catch (error) {
    showError(error instanceof Error ? error.message : "Selected records could not be queued.");
  }
});
el.syncInstitution.addEventListener("click", async () => {
  el.syncInstitution.disabled = true;
  el.institutionStatus.textContent = "Synchronizing explicitly queued evidence…";
  try {
    const result = await historyFetch("/api/v1/institution/sync-now", { method: "POST" });
    el.institutionStatus.textContent = `${result.completed} record(s) synchronized · ${result.pending_after_error} still pending`;
  } catch (error) {
    el.institutionStatus.textContent = error instanceof Error ? error.message : "Institution sync is unavailable.";
  }
  await loadInstitutionStatus();
});
el.importButton.addEventListener("click", () => el.importFile.click());
el.importFile.addEventListener("change", async () => {
  if (!el.importFile.files[0]) return;
  const form = new FormData(); form.append("file", el.importFile.files[0]);
  try {
    const result = await historyFetch("/api/v1/history/import", { method: "POST", body: form });
    showError(); el.count.textContent = `Imported ${result.imported_count} records`; await loadHistory();
  } catch (error) { showError(error instanceof Error ? error.message : "History import failed."); }
  finally { el.importFile.value = ""; }
});
el.clear.addEventListener("click", async () => {
  if (!await confirmAction("Clear terminal History?", "Delete all completed, failed, and cancelled History archives? Named exports remain.", "Clear History")) return;
  try { await historyFetch("/api/v1/history", { method: "DELETE" }); historyState.offset = 0; historyState.checked.clear(); updateBulkActions(); await loadHistory(); }
  catch (error) { showError(error instanceof Error ? error.message : "History could not be cleared."); }
});
el.closeDetail.addEventListener("click", () => el.dialog.close());
el.copyResult.addEventListener("click", async () => {
  if (!historyState.selected) return;
  try { await navigator.clipboard.writeText(historyState.selected.result?.markdown || historyState.selected.primary_output || JSON.stringify(historyState.selected.result, null, 2)); el.copyResult.textContent = "Copied"; window.setTimeout(() => { el.copyResult.textContent = "Copy output"; }, 1200); }
  catch { el.copyResult.textContent = "Copy failed"; }
});
el.pinResult.addEventListener("click", async () => { if (historyState.selected) { await setPinned(historyState.selected.job_id, !historyState.selected.pinned); historyState.selected.pinned = !historyState.selected.pinned; el.pinResult.textContent = historyState.selected.pinned ? "Unpin" : "Pin"; } });
el.rerunResult.addEventListener("click", () => { if (historyState.selected) rememberRerun(historyState.selected); });
el.revealResult.addEventListener("click", async () => { if (historyState.selected) await reveal(historyState.selected.job_id); });

updateBulkActions();
loadHistory();
loadInstitutionStatus();

"use strict";

(() => {
  /** @type {HTMLButtonElement | null} */
  const button = document.querySelector("#maintenance-button");
  /** @type {HTMLDialogElement | null} */
  const dialog = document.querySelector("#maintenance-dialog");
  if (!button || !dialog || typeof dialog.showModal !== "function") return;

  /** @type {HTMLButtonElement} */
  const closeButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#maintenance-close"));
  /** @type {HTMLButtonElement} */
  const checkButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#maintenance-check"));
  /** @type {HTMLButtonElement} */
  const applyButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#maintenance-apply"));
  /** @type {HTMLButtonElement} */
  const cancelButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#maintenance-cancel"));
  /** @type {HTMLElement} */
  const status = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-status"));
  /** @type {HTMLElement} */
  const inventory = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-inventory"));
  /** @type {HTMLElement} */
  const ready = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-ready"));
  /** @type {HTMLElement} */
  const blocked = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-blocked"));
  /** @type {HTMLElement} */
  const progressPanel = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-progress"));
  /** @type {HTMLElement} */
  const progressStage = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-progress-stage"));
  /** @type {HTMLElement} */
  const progressValue = /** @type {HTMLElement} */ (dialog.querySelector("#maintenance-progress-value"));
  /** @type {HTMLProgressElement} */
  const progressBar = /** @type {HTMLProgressElement} */ (dialog.querySelector("#maintenance-progress-bar"));
  let review = null;
  let pollTimer = 0;
  let checkController = null;
  let checkGeneration = 0;
  let activeJobId = "";
  let lastJobProgress = 0;

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("error-callout", isError);
  }

  /**
   * Render a truthful maintenance state. A null value intentionally keeps the
   * native progress control indeterminate while the signed channel is checked.
   * @param {"checking" | "active" | "complete" | "warning" | "error"} state
   * @param {string} stage
   * @param {number | null} value
   */
  function setProgress(state, stage, value) {
    progressPanel.hidden = false;
    progressPanel.className = `maintenance-progress is-${state}`;
    progressPanel.setAttribute("aria-busy", String(state === "checking" || state === "active"));
    progressStage.textContent = stage;

    if (value === null) {
      progressBar.removeAttribute("value");
      progressBar.setAttribute("aria-valuetext", stage);
      progressValue.textContent = "Checking";
      return;
    }

    const normalized = Math.max(0, Math.min(100, Math.round(value)));
    progressBar.value = normalized;
    progressBar.setAttribute("aria-valuetext", `${stage}: ${normalized}%`);
    progressValue.textContent = `${normalized}%`;
  }

  function clearProgress() {
    progressPanel.hidden = true;
    progressPanel.className = "maintenance-progress";
    progressPanel.setAttribute("aria-busy", "false");
    progressBar.value = 0;
    progressBar.removeAttribute("aria-valuetext");
    progressStage.textContent = "Waiting to check for updates.";
    progressValue.textContent = "";
  }

  function setRows(list, entries, emptyMessage, formatter) {
    list.replaceChildren();
    if (!entries.length) {
      const item = document.createElement("li");
      item.className = "maintenance-empty";
      item.textContent = emptyMessage;
      list.append(item);
      return;
    }
    entries.forEach((entry) => {
      const item = document.createElement("li");
      formatter(item, entry);
      list.append(item);
    });
  }

  function componentRow(item, entry) {
    const name = document.createElement("strong");
    name.textContent = entry.name;
    const detail = document.createElement("span");
    detail.textContent = `Installed: ${entry.installed_version} · ${entry.health}`;
    const note = document.createElement("small");
    note.textContent = entry.detail;
    item.append(name, detail, note);
  }

  function updateRow(item, entry) {
    const name = document.createElement("strong");
    name.textContent = entry.name;
    const detail = document.createElement("span");
    detail.textContent = `${entry.installed_version} → ${entry.target_version}`;
    item.append(name, detail);
  }

  function blockedRow(item, entry) {
    updateRow(item, entry);
    const note = document.createElement("small");
    note.textContent = entry.reason;
    item.append(note);
  }

  function setOnlineState() {
    const online = navigator.onLine;
    button.disabled = !online;
    button.title = online
      ? "Review signed runtime maintenance updates"
      : "Maintenance requires an internet connection";
    if (!online && dialog.open) {
      applyButton.disabled = true;
      if (checkController) checkController.abort();
      setProgress("error", "Maintenance is unavailable while offline.", 0);
      setStatus("Maintenance is unavailable while this browser is offline.", true);
    }
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "Maintenance request failed");
    return body;
  }

  function finishCheck(generation) {
    if (generation !== checkGeneration) return;
    checkController = null;
    checkButton.disabled = Boolean(activeJobId) || !navigator.onLine;
  }

  async function check() {
    if (!navigator.onLine) {
      setOnlineState();
      return;
    }
    if (checkController || activeJobId) return;

    window.clearTimeout(pollTimer);
    review = null;
    applyButton.disabled = true;
    cancelButton.hidden = true;
    checkButton.disabled = true;
    const generation = ++checkGeneration;
    checkController = new AbortController();
    setProgress("checking", "Checking the signed maintenance channel…", null);
    setStatus("Checking the signed maintenance channel…");
    try {
      const data = await request("/api/v1/maintenance/check", {
        method: "POST",
        signal: checkController.signal,
      });
      if (generation !== checkGeneration || !dialog.open) return;
      setRows(inventory, data.inventory || [], "No component inventory is available.", componentRow);
      setRows(ready, data.ready_to_update || [], "No compatible approved updates are ready.", updateRow);
      setRows(blocked, data.blocked || [], "No blocked update candidates.", blockedRow);
      review = data.can_apply ? data : null;
      const available = data.channel?.status === "available";
      applyButton.disabled = !review || !navigator.onLine;
      setProgress(available ? "complete" : "error", available ? "Signed channel check complete." : "Signed channel check failed.", 100);
      setStatus(data.channel?.message || "Maintenance channel checked.", !available);
    } catch (error) {
      if (generation !== checkGeneration || error.name === "AbortError") return;
      setRows(inventory, [], "The local inventory could not be loaded.", componentRow);
      setRows(ready, [], "No update was reviewed.", updateRow);
      setRows(blocked, [], "No compatibility result is available.", blockedRow);
      setProgress("error", "Signed channel check failed.", 0);
      setStatus(error.message || "Maintenance is unavailable.", true);
    } finally {
      finishCheck(generation);
    }
  }

  function jobProgressState(job) {
    if (job.status === "failed") return "error";
    if (job.status === "cancelled" || job.status === "restart_required") return "warning";
    if (job.status === "completed") return "complete";
    return "active";
  }

  function jobProgressLabel(job) {
    const labels = {
      queued: "Maintenance queued.",
      download: "Downloading approved updates.",
      validation: "Validating staged runtime.",
      restart: "Restart required to complete maintenance.",
      cancelled: "Maintenance cancelled.",
      failed: "Maintenance failed.",
      completed: "Maintenance complete.",
    };
    return labels[job.stage] || job.message || "Maintenance is running.";
  }

  function finishJob(job) {
    activeJobId = "";
    cancelButton.hidden = true;
    checkButton.disabled = !navigator.onLine;
    if (job.status === "restart_required") {
      setStatus(`${job.message} Your OCR files, History, output, and logs are unchanged.`);
    }
  }

  async function poll(jobId) {
    try {
      const job = await request(`/api/v1/maintenance/jobs/${encodeURIComponent(jobId)}`);
      if (activeJobId && job.job_id !== activeJobId) return;
      lastJobProgress = Number.isFinite(Number(job.progress)) ? Number(job.progress) : lastJobProgress;
      const terminal = ["cancelled", "failed", "restart_required", "completed"].includes(job.status);
      setProgress(jobProgressState(job), jobProgressLabel(job), lastJobProgress);
      setStatus(job.message || "Maintenance is running.", job.status === "failed");
      cancelButton.dataset.jobId = job.job_id;
      cancelButton.hidden = !job.cancellable;
      applyButton.disabled = true;
      if (!terminal) {
        pollTimer = window.setTimeout(() => poll(jobId), 900);
      } else {
        finishJob(job);
      }
    } catch (error) {
      cancelButton.hidden = true;
      setProgress("error", "Maintenance status could not be read.", lastJobProgress);
      setStatus(error.message || "The maintenance status could not be read.", true);
    }
  }

  async function apply() {
    if (!review || !navigator.onLine || activeJobId) return;
    applyButton.disabled = true;
    checkButton.disabled = true;
    lastJobProgress = 0;
    setProgress("active", "Starting reviewed maintenance…", 0);
    setStatus("Starting reviewed maintenance…");
    try {
      const job = await request("/api/v1/maintenance/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          manifest_version: review.channel.manifest_version,
          review_token: review.review_token,
        }),
      });
      activeJobId = job.job_id;
      cancelButton.dataset.jobId = job.job_id;
      cancelButton.hidden = !job.cancellable;
      poll(job.job_id);
    } catch (error) {
      checkButton.disabled = !navigator.onLine;
      setProgress("error", "Maintenance could not start.", 0);
      setStatus(error.message || "Maintenance could not start.", true);
    }
  }

  async function cancel() {
    const currentJobId = cancelButton.dataset.jobId || activeJobId;
    if (!currentJobId) return;
    cancelButton.disabled = true;
    try {
      await request(`/api/v1/maintenance/jobs/${encodeURIComponent(currentJobId)}`, { method: "DELETE" });
      setProgress("active", "Cancellation requested; finishing the current safe step.", lastJobProgress);
      setStatus("Cancellation requested. The current safe validation step will finish first.");
      window.clearTimeout(pollTimer);
      poll(currentJobId);
    } catch (error) {
      setProgress("error", "Maintenance could not be cancelled.", lastJobProgress);
      setStatus(error.message || "Maintenance could not be cancelled.", true);
    } finally {
      cancelButton.disabled = false;
    }
  }

  button.addEventListener("click", () => {
    dialog.showModal();
    check();
  });
  closeButton.addEventListener("click", () => dialog.close());
  checkButton.addEventListener("click", check);
  applyButton.addEventListener("click", apply);
  cancelButton.addEventListener("click", cancel);
  dialog.addEventListener("close", () => {
    window.clearTimeout(pollTimer);
    pollTimer = 0;
    checkGeneration += 1;
    if (checkController) checkController.abort();
    checkController = null;
    clearProgress();
    button.focus();
  });
  window.addEventListener("online", setOnlineState);
  window.addEventListener("offline", setOnlineState);
  setOnlineState();
})();

"use strict";

(() => {
  /** @type {HTMLButtonElement | null} */
  const openButton = document.querySelector("#quit-app-button");
  /** @type {HTMLDialogElement | null} */
  const dialog = document.querySelector("#quit-app-dialog");
  const token = document.querySelector('meta[name="predixalearn-control-token"]')?.getAttribute("content");
  if (!openButton || !dialog || !token || typeof dialog.showModal !== "function") return;

  /** @type {HTMLButtonElement} */
  const closeButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#quit-app-close"));
  /** @type {HTMLButtonElement} */
  const cancelButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#quit-app-cancel"));
  /** @type {HTMLButtonElement} */
  const confirmButton = /** @type {HTMLButtonElement} */ (dialog.querySelector("#quit-app-confirm"));
  /** @type {HTMLElement} */
  const status = /** @type {HTMLElement} */ (dialog.querySelector("#quit-app-status"));

  function close() {
    dialog.close();
    openButton.focus();
  }

  openButton.addEventListener("click", () => {
    status.textContent = "Finish or cancel any OCR work before quitting.";
    status.classList.remove("error-callout");
    confirmButton.disabled = false;
    dialog.showModal();
  });
  closeButton.addEventListener("click", close);
  cancelButton.addEventListener("click", close);
  confirmButton.addEventListener("click", async () => {
    confirmButton.disabled = true;
    status.classList.remove("error-callout");
    status.textContent = "Closing the local PredixaLearn service…";
    try {
      const response = await fetch("/api/v1/app/quit", {
        method: "POST",
        headers: { "X-PredixaLearn-Control": token },
        cache: "no-store",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "PredixaLearn could not close safely.");
      status.textContent = "PredixaLearn is closing. You can close this browser tab.";
      openButton.disabled = true;
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "PredixaLearn could not close safely.";
      status.classList.add("error-callout");
      confirmButton.disabled = false;
    }
  });
})();

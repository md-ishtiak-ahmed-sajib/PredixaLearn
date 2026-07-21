"use strict";

async function updateServiceHealth() {
  /** @type {any} */
  const badge = document.querySelector("#health-badge");
  /** @type {any} */
  const detail = document.querySelector("#health-detail");
  if (!badge) return;
  try {
    const response = await fetch("/api/v1/health", { cache: "no-store" });
    if (!response.ok) throw new Error("Health request failed");
    const health = await response.json();
    const ready = health.status === "ready";
    const initializing = health.status === "initializing";
    badge.textContent = ready ? "Service ready" : initializing ? "Initializing OCR" : "Service degraded";
    badge.className = `health health-${health.status}`;
    badge.title = health.issues.join("; ");
    if (detail) {
      detail.textContent = ready
        ? `Ready on ${health.gpu.device}. Local OCR and document export are available.`
        : initializing
          ? "The studio is available while the OCR engine finishes initializing."
          : health.issues.join("; ") || "The OCR runtime reported a degraded state.";
      detail.classList.toggle("error-callout", !ready && !initializing);
    }
    window.predixalearnHealth = health;
    window.dispatchEvent(new CustomEvent("predixalearn:health", { detail: health }));
    if (initializing) window.setTimeout(updateServiceHealth, 1000);
  } catch {
    badge.textContent = "Service unavailable";
    badge.className = "health health-degraded";
    badge.title = "The health endpoint could not be reached";
    if (detail) {
      detail.textContent = "The service health endpoint could not be reached.";
      detail.classList.add("error-callout");
    }
    window.predixalearnHealth = { status: "degraded", warmup: { status: "failed" } };
    window.dispatchEvent(new CustomEvent("predixalearn:health", { detail: window.predixalearnHealth }));
  }
}

updateServiceHealth();

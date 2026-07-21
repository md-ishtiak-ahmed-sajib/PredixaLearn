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

function setupNavigation() {
  const toggle = document.querySelector("#nav-toggle");
  const navigation = document.querySelector("#primary-nav");
  const topbar = document.querySelector(".topbar");
  if (!(toggle instanceof HTMLButtonElement) || !(navigation instanceof HTMLElement) || !(topbar instanceof HTMLElement)) return;

  const mobileQuery = window.matchMedia("(max-width: 1100px)");
  const disclosures = Array.from(navigation.querySelectorAll("details.nav-group"));
  const closeDisclosures = () => {
    disclosures.filter((group) => group.open).forEach((group) => { group.open = false; });
  };
  const setOpen = (open) => {
    const shouldOpen = open && mobileQuery.matches;
    topbar.classList.toggle("nav-open", shouldOpen);
    toggle.setAttribute("aria-expanded", String(shouldOpen));
    toggle.querySelector(".sr-only").textContent = shouldOpen ? "Close navigation" : "Open navigation";
    if (!shouldOpen) closeDisclosures();
  };

  toggle.addEventListener("click", () => setOpen(!topbar.classList.contains("nav-open")));
  disclosures.forEach((group) => {
    group.addEventListener("toggle", () => {
      if (!group.open) return;
      disclosures.filter((other) => other !== group && other.open).forEach((other) => { other.open = false; });
    });
  });
  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) setOpen(false);
  });
  document.addEventListener("pointerdown", (event) => {
    const target = event.target;
    if (mobileQuery.matches && topbar.classList.contains("nav-open") && target instanceof Node && !topbar.contains(target)) setOpen(false);
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && topbar.classList.contains("nav-open")) {
      setOpen(false);
      toggle.focus();
    }
  }, true);
  mobileQuery.addEventListener("change", () => setOpen(false));
}

setupNavigation();
updateServiceHealth();

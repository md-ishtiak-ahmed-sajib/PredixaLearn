// @ts-check

/** @template {Element} T @param {string} selector @returns {T} */
function element(selector) {
  const value = document.querySelector(selector);
  if (!value) throw new Error(`Missing revision element: ${selector}`);
  return /** @type {T} */ (value);
}

/** @param {string} path @returns {Promise<any>} */
async function api(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (response.status === 401) { location.href = "/auth/login"; throw new Error("Authentication required"); }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail?.message || payload?.detail || "Request failed");
  return payload;
}

/** @param {string} message */
function announce(message) { const target = /** @type {HTMLElement} */ (element("#revision-status")); target.textContent = message; target.classList.toggle("hidden", !message); }

/** @param {Array<any>} packs */
function renderPacks(packs) {
  const root = /** @type {HTMLElement} */ (element("#revision-packs")); root.replaceChildren();
  for (const pack of packs) {
    const article = document.createElement("article"); article.className = "card";
    const title = document.createElement("h3"); title.textContent = String(pack.title || "Revision pack"); article.append(title);
    const intro = document.createElement("p"); intro.textContent = String(pack.payload?.introduction || "Teacher-approved practice"); article.append(intro);
    const list = document.createElement("ol");
    for (const item of pack.payload?.items || []) { const row = document.createElement("li"); const heading = document.createElement("strong"); heading.textContent = String(item.text || "Practice question"); row.append(heading); if (item.accessibility_description) { const description = document.createElement("p"); description.textContent = String(item.accessibility_description); row.append(description); } list.append(row); }
    article.append(list); root.append(article);
  }
  if (!packs.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No teacher-approved revision packs are available for your courses."; root.append(empty); }
}

async function load() {
  const [packs, progress] = await Promise.all([api("/api/v2/student/revision-packs"), api("/api/v2/revision-progress?limit=100")]);
  /** @type {HTMLElement} */ (element("#revision-identity")).textContent = `Signed in learner context: ${packs.learner_id || "pseudonymous learner"}`;
  renderPacks(packs.items || []);
  const root = /** @type {HTMLElement} */ (element("#revision-progress")); root.replaceChildren();
  for (const item of progress.items || []) { const card = document.createElement("article"); card.className = "card"; const title = document.createElement("h3"); title.textContent = String(item.title || "Revision progress"); const meta = document.createElement("p"); meta.textContent = String(item.payload?.summary || "Private progress saved"); card.append(title, meta); root.append(card); }
  if (!(progress.items || []).length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No private progress has been recorded yet."; root.append(empty); }
  announce("");
}

load().catch((error) => announce(error instanceof Error ? error.message : "Revision dashboard unavailable"));

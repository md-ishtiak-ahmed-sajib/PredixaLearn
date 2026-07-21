// @ts-check
export {};

const byId = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
async function api(url, options = {}) { const response = await fetch(url, { cache: "no-store", ...options }); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`); return body; }
function setStatus(text, error = false) { const target = byId("revision-status-message"); target.textContent = text; target.classList.toggle("error-callout", error); }
function node(tag, className, text) { const value = document.createElement(tag); value.className = className || ""; if (text !== undefined) value.textContent = text; return value; }

async function loadPacks() {
  const data = await api("/api/v1/revision-packs?audience=student"); const root = byId("revision-packs"); root.replaceChildren();
  for (const pack of data.items) {
    const card = node("article", "revision-pack"); card.append(node("p", "eyebrow", "English · teacher-approved"), node("h3", "", pack.title));
    const intro = pack.payload?.introduction; if (intro) card.append(node("p", "", intro));
    card.append(node("p", "", `${pack.source_item_ids.length} approved question-bank item(s). Teacher notes, hidden marks, and unpublished questions are excluded from this view.`));
    const actions = node("div", "compact-actions"); for (const format of ["markdown", "json", "docx"]) { const link = node("a", "quiet-button", `Download ${format.toUpperCase()}`); link.href = `/api/v1/revision-packs/${pack.pack_id}/export?format=${format}`; actions.append(link); } card.append(actions); root.append(card);
  }
  if (!data.items.length) root.append(node("p", "empty-copy", "No teacher-approved revision packs yet."));
}
byId("revision-refresh").addEventListener("click", () => loadPacks().catch((error) => setStatus(error.message, true)));
byId("revision-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const ids = /** @type {HTMLTextAreaElement} */ (byId("revision-items")).value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const payload = { title: /** @type {HTMLInputElement} */ (byId("revision-title")).value, language: /** @type {HTMLInputElement} */ (byId("revision-language")).value, source_item_ids: ids, status: /** @type {HTMLSelectElement} */ (byId("revision-status")).value, payload: { introduction: /** @type {HTMLTextAreaElement} */ (byId("revision-introduction")).value, statement: "Teacher-reviewed historical practice; not a future-exam prediction." } };
  try { await api("/api/v1/revision-packs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); await loadPacks(); setStatus("Revision pack saved. Student-safe preview includes only approved packs and approved bank items."); } catch (error) { setStatus(error.message, true); }
});
loadPacks().catch((error) => setStatus(error.message, true));

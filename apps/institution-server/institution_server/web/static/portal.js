// @ts-check

/** @template {Element} T @param {string} selector @returns {T} */
function element(selector) {
  const value = document.querySelector(selector);
  if (!value) throw new Error(`Missing portal element: ${selector}`);
  return /** @type {T} */ (value);
}

/** @param {unknown} error @returns {string} */
const errorMessage = (error) => error instanceof Error ? error.message : "Request failed";
let csrfToken = "";

/** @param {string} path @param {RequestInit} [options] @returns {Promise<any>} */
async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (csrfToken && options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    location.href = "/auth/login";
    throw new Error("Authentication required");
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.detail?.message || "Request failed");
  return payload;
}

/** @param {string} message */
function announce(message) {
  const status = /** @type {HTMLElement} */ (element("#portal-status"));
  status.textContent = message;
  status.classList.toggle("hidden", !message);
}

/** @param {unknown} value */
const text = (value) => String(value ?? "");

/** @param {HTMLElement} target @param {Array<any>} items @param {(article: HTMLElement, item: any) => void} [decorate] */
function renderCards(target, items, decorate) {
  target.replaceChildren(...items.map((item) => {
    const article = document.createElement("article");
    article.className = "card";
    const title = document.createElement("h3");
    title.textContent = text(item.title);
    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = `${text(item.type)} · ${text(item.status)} · version ${text(item.version)}`;
    article.append(title, meta);
    decorate?.(article, item);
    return article;
  }));
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No authorized records match this view.";
    target.replaceChildren(empty);
  }
}

const identity = /** @type {HTMLElement} */ (element("#identity"));
api("/api/v2/me")
  .then((me) => {
    csrfToken = me.csrf_token || "";
    identity.textContent = `${me.tenant_id} · ${me.roles.join(", ")}`;
  })
  .catch((error) => { identity.textContent = errorMessage(error); });

/** @param {HTMLElement} article @param {any} review */
function decorateReview(article, review) {
  const actions = document.createElement("div");
  actions.className = "card-actions";
  for (const [label, action] of [["Comment", "comment"], ["Approve", "approved"], ["Request changes", "changes_requested"], ["Reject", "rejected"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => openReviewDialog(review, action));
    actions.append(button);
  }
  article.append(actions);
}

/** @type {Record<string, () => Promise<void>>} */
const loaders = {
  reviews: async () => {
    const data = await api("/api/v2/reviews");
    renderCards(element("#review-results"), data.items, decorateReview);
  },
  curriculum: async () => {
    const [curricula, rubrics, objectives] = await Promise.all([
      api("/api/v2/curricula/versions"),
      api("/api/v2/rubric-versions"),
      api("/api/v2/learning-objectives"),
    ]);
    renderCards(element("#curriculum-results"), [...curricula.items, ...rubrics.items, ...objectives.items]);
  },
  answers: async () => {
    const data = await api("/api/v2/answer-scripts");
    renderCards(element("#answer-results"), data.items);
  },
  accessibility: async () => {
    const data = await api("/api/v2/accessibility-descriptions");
    renderCards(element("#accessibility-results"), data.items);
  },
  datasets: async () => {
    const data = await api("/api/v2/datasets");
    renderCards(element("#dataset-results"), data.items);
  },
};

let currentView = "archive";
/** @param {string} view */
async function loadView(view) {
  currentView = view;
  if (!loaders[view]) return;
  try {
    await loaders[view]();
    announce("");
  } catch (error) {
    announce(errorMessage(error));
  }
}

document.querySelectorAll("[data-view]").forEach((candidate) => {
  const button = /** @type {HTMLButtonElement} */ (candidate);
  button.addEventListener("click", async () => {
    document.querySelectorAll("[data-view]").forEach((item) => item.removeAttribute("aria-current"));
    button.setAttribute("aria-current", "page");
    document.querySelectorAll("[id$='-view']").forEach((panel) => panel.classList.add("hidden"));
    const view = button.dataset.view || "archive";
    document.querySelector(`#${view}-view`)?.classList.remove("hidden");
    await loadView(view);
  });
});

const searchForm = /** @type {HTMLFormElement} */ (element("#search-form"));
searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const status = /** @type {HTMLElement} */ (element("#search-status"));
  status.textContent = "Searching authorized evidence…";
  try {
    const query = /** @type {HTMLInputElement} */ (element("#search-query")).value;
    const courseId = /** @type {HTMLInputElement} */ (element("#search-course")).value.trim();
    const statusFilter = /** @type {HTMLInputElement} */ (element("#search-status-filter")).value.trim();
    const resourceTypes = /** @type {HTMLInputElement} */ (element("#search-types")).value
      .split(",").map((value) => value.trim()).filter(Boolean);
    const semantic = /** @type {HTMLInputElement} */ (element("#search-semantic")).checked;
    const result = await api("/api/v2/search", {
      method: "POST",
      body: JSON.stringify({
        query,
        course_id: courseId || null,
        statuses: statusFilter ? [statusFilter] : [],
        resource_types: resourceTypes,
        semantic,
      }),
    });
    renderCards(element("#search-results"), result.items);
    status.textContent = `${result.items.length} authorized result(s)`;
  } catch (error) {
    status.textContent = errorMessage(error);
  }
});

const reviewDialog = /** @type {HTMLDialogElement} */ (element("#review-dialog"));
const reviewForm = /** @type {HTMLFormElement} */ (element("#review-action-form"));
let reviewAction = "";
/** @type {any} */
let selectedReview = null;

/** @param {any} review @param {string} action */
function openReviewDialog(review, action) {
  selectedReview = review;
  reviewAction = action;
  element("#review-dialog-title").textContent = action === "comment" ? "Add review comment" : "Record review decision";
  element("#review-dialog-context").textContent = review.title;
  /** @type {HTMLTextAreaElement} */ (element("#review-text")).value = "";
  reviewDialog.showModal();
  /** @type {HTMLTextAreaElement} */ (element("#review-text")).focus();
}

element("[data-close-dialog]").addEventListener("click", () => reviewDialog.close());
reviewForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedReview) return;
  const value = /** @type {HTMLTextAreaElement} */ (element("#review-text")).value.trim();
  if (!value) return;
  try {
    if (reviewAction === "comment") {
      await api(`/api/v2/reviews/${selectedReview.id}/comments`, {
        method: "POST", body: JSON.stringify({ text: value, mentions: [] }),
      });
    } else {
      await api(`/api/v2/reviews/${selectedReview.id}/decision`, {
        method: "POST",
        headers: { "If-Match": selectedReview.etag },
        body: JSON.stringify({ decision: reviewAction, rationale: value }),
      });
    }
    reviewDialog.close();
    await loadView("reviews");
    announce("Review update saved and audited.");
  } catch (error) {
    announce(errorMessage(error));
  }
});

let refreshTimer = 0;
const events = new EventSource("/api/v2/events");
events.addEventListener("audit", () => {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => loadView(currentView), 250);
});
events.onerror = () => { /* EventSource reconnects; API views remain usable. */ };

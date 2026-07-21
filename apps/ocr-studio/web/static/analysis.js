// @ts-check

import { fetchJson } from "./modules/api.js";

/** @type {Record<string, any>} */
const el = {
  history: document.querySelector("#analysis-history"),
  subject: document.querySelector("#analysis-subject"),
  level: document.querySelector("#analysis-level"),
  taxonomy: document.querySelector("#analysis-taxonomy"),
  consent: document.querySelector("#analysis-consent"),
  local: document.querySelector("#analysis-local"),
  practiceGenerate: document.querySelector("#analysis-practice-generate"),
  cloud: document.querySelector("#analysis-cloud"),
  compare: document.querySelector("#analysis-compare"),
  status: document.querySelector("#analysis-status"),
  title: document.querySelector("#analysis-overview-title"),
  modelStatus: document.querySelector("#analysis-model-status"),
  facts: document.querySelector("#analysis-facts"),
  priorities: document.querySelector("#analysis-priorities"),
  practice: document.querySelector("#analysis-practice"),
  practiceList: document.querySelector("#analysis-practice-list"),
  questionList: document.querySelector("#analysis-question-list"),
  sourceMeta: document.querySelector("#analysis-source-meta"),
  sourcePreview: document.querySelector("#analysis-source-preview"),
  downloads: document.querySelector("#analysis-downloads"),
  downloadMarkdown: document.querySelector("#analysis-download-markdown"),
  downloadJson: document.querySelector("#analysis-download-json"),
  downloadDocx: document.querySelector("#analysis-download-docx"),
};

const state = { jobId: null, analysis: null, loading: false };

function setStatus(message, error = false) {
  el.status.textContent = message;
  el.status.classList.toggle("error-callout", error);
}

function selectedIds() {
  return Array.from(el.history.selectedOptions).map((option) => option.value).filter(Boolean);
}

function taxonomy() {
  return el.taxonomy.value.split(/\r?\n|,/).map((value) => value.trim()).filter(Boolean);
}

function clearNode(node) { node.replaceChildren(); }

function makeText(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function effective(question, field) {
  if (question?.teacher_override && Object.hasOwn(question.teacher_override, field)) return question.teacher_override[field];
  return question?.ai?.[field] ?? null;
}

function renderFacts(analysis) {
  clearNode(el.facts);
  const details = [
    ["Questions", analysis?.analysis?.question_count ?? analysis?.questions?.length ?? 0],
    ["Explicit marks", analysis?.analysis?.explicit_marks_total ?? 0],
    ["Model", analysis?.model?.model || "Local evidence only"],
    ["Cloud consent", analysis?.model?.consent_to_cloud ? "Granted for this run" : "Not granted"],
  ];
  details.forEach(([label, value]) => {
    const item = document.createElement("div");
    item.append(makeText("dt", "", String(label)), makeText("dd", "", String(value)));
    el.facts.append(item);
  });
}

function renderPriorities(analysis) {
  clearNode(el.priorities);
  const priorities = analysis?.analysis?.revision_priorities || [];
  const recurring = analysis?.analysis?.recurring_topics || [];
  if (!priorities.length && !recurring.length) {
    el.priorities.append(makeText("p", "empty-copy", "Topics and revision priorities appear after teacher or GPT classification."));
    return;
  }
  if (priorities.length) {
    const heading = makeText("h3", "", "Revision priorities");
    const list = document.createElement("ul");
    priorities.forEach((item) => list.append(makeText("li", "", `${item.topic}: ${item.reason}`)));
    el.priorities.append(heading, list);
  }
  if (recurring.length) {
    const heading = makeText("h3", "", "Repeated concepts across selected papers");
    const list = document.createElement("ul");
    recurring.forEach((item) => list.append(makeText("li", "", `${item.topic}: ${item.occurrences} occurrences`)));
    el.priorities.append(heading, list);
  }
}

function renderPractice(analysis) {
  clearNode(el.practiceList);
  const suggestions = Array.isArray(analysis?.practice_items) ? analysis.practice_items : [];
  el.practice.classList.toggle("hidden", !suggestions.length);
  suggestions.forEach((item) => {
    const source = String(item?.source_question_id || "source question");
    const prompt = String(item?.prompt || "");
    el.practiceList.append(makeText("li", "", `${source}: ${prompt}`));
  });
}

function sourceBox(question) {
  const value = question?.source_bbox;
  return Array.isArray(value) && value.length === 4 && value.every((item) => Number.isFinite(Number(item))) ? value.map(Number) : null;
}

function showSource(question) {
  clearNode(el.sourcePreview);
  if (!state.jobId || !question) return;
  const page = Number(question.source_page);
  const source = document.createElement("div");
  source.className = "source-page-frame";
  const image = document.createElement("img");
  image.src = `/api/v1/history/${encodeURIComponent(state.jobId)}/source-pages/${page}`;
  image.alt = `Watermarked local source preview for page ${page}`;
  image.addEventListener("error", () => {
    source.replaceWith(makeText("p", "empty-copy", "This earlier run has no retained source preview. Reprocess it to create one."));
  });
  source.append(image);
  const bbox = sourceBox(question);
  if (bbox) {
    const highlight = document.createElement("span");
    highlight.className = "source-highlight";
    highlight.style.left = `${bbox[0] * 100}%`;
    highlight.style.top = `${bbox[1] * 100}%`;
    highlight.style.width = `${Math.max(0, bbox[2] - bbox[0]) * 100}%`;
    highlight.style.height = `${Math.max(0, bbox[3] - bbox[1]) * 100}%`;
    highlight.setAttribute("aria-hidden", "true");
    source.append(highlight);
  }
  const crop = document.createElement("img");
  crop.className = "question-crop";
  crop.src = `/api/v1/history/${encodeURIComponent(state.jobId)}/analysis/questions/${encodeURIComponent(question.question_id)}/crop`;
  crop.alt = `Local crop for ${question.display_number}`;
  crop.addEventListener("error", () => crop.remove());
  el.sourcePreview.append(source, crop);
  el.sourceMeta.textContent = `${question.display_number} · source page ${page} · lines ${question.source_line_ids.join(", ")} · OCR confidence ${question.ocr_confidence ?? "not available"} · AI confidence ${question.ai?.classification_confidence ?? "not available"}`;
}

function selectDifficulty(value) {
  const select = document.createElement("select");
  ["unclassified", "low", "medium", "high"].forEach((difficulty) => {
    const option = document.createElement("option");
    option.value = difficulty;
    option.textContent = difficulty[0].toUpperCase() + difficulty.slice(1);
    option.selected = difficulty === (value || "unclassified");
    select.append(option);
  });
  return select;
}

function createQuestionCard(question) {
  const card = document.createElement("article");
  card.className = "analysis-question-card";
  card.tabIndex = 0;
  card.append(makeText("h3", "", String(question.display_number)), makeText("p", "question-text", String(question.text)));
  const facts = makeText("p", "question-facts", `Page ${question.source_page} · OCR ${question.ocr_confidence ?? "—"} · marks ${question.marks ?? "not detected"}`);
  card.append(facts);
  const editor = document.createElement("div");
  editor.className = "question-editor";
  const topic = document.createElement("input");
  topic.type = "text"; topic.value = effective(question, "topic") || ""; topic.placeholder = "Topic"; topic.setAttribute("aria-label", `${question.display_number} topic`);
  const difficulty = selectDifficulty(effective(question, "difficulty"));
  difficulty.setAttribute("aria-label", `${question.display_number} difficulty`);
  const marks = document.createElement("input");
  marks.type = "number"; marks.min = "0"; marks.max = "100"; marks.value = String(question.marks ?? ""); marks.placeholder = "Marks"; marks.setAttribute("aria-label", `${question.display_number} marks`);
  const save = makeText("button", "quiet-button", "Save teacher review");
  save.type = "button";
  save.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (!state.jobId) return;
    save.disabled = true;
    try {
      const payload = { topic: topic.value || null, difficulty: difficulty.value, review_status: "teacher_reviewed" };
      if (marks.value !== "") payload.marks = Number(marks.value);
      const updated = await fetchJson(`/api/v1/history/${encodeURIComponent(state.jobId)}/analysis/questions/${encodeURIComponent(question.question_id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      state.analysis = updated;
      renderAnalysis(updated);
      setStatus(`${question.display_number} teacher review saved locally.`);
    } catch (error) { setStatus(error instanceof Error ? error.message : "Teacher review could not be saved.", true); }
    finally { save.disabled = false; }
  });
  editor.append(topic, difficulty, marks, save);
  card.append(editor);
  card.addEventListener("click", () => showSource(question));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showSource(question); }
  });
  return card;
}

function renderQuestions(analysis) {
  clearNode(el.questionList);
  const questions = Array.isArray(analysis?.questions) ? analysis.questions : [];
  if (!questions.length) {
    el.questionList.append(makeText("p", "empty-copy", "No question boundaries could be prepared from this OCR result."));
    return;
  }
  questions.forEach((question) => el.questionList.append(createQuestionCard(question)));
  showSource(questions[0]);
}

function renderDownloads() {
  const show = Boolean(state.jobId && state.analysis?.updated_at);
  el.downloads.classList.toggle("hidden", !show);
  if (!show) return;
  const job = encodeURIComponent(state.jobId);
  el.downloadMarkdown.href = `/api/v1/history/${job}/analysis/download?format=markdown`;
  el.downloadJson.href = `/api/v1/history/${job}/analysis/download?format=json`;
  el.downloadDocx.href = `/api/v1/history/${job}/analysis/download?format=docx`;
}

function renderAnalysis(analysis) {
  state.analysis = analysis;
  el.practiceGenerate.disabled = !state.jobId;
  el.title.textContent = analysis?.subject ? `${analysis.subject} past paper` : "Past paper analysis";
  const cloud = analysis?.model?.consent_to_cloud;
  el.modelStatus.textContent = analysis?.model?.model || "Local evidence";
  el.modelStatus.className = `status-pill ${cloud ? "status-completed" : "status-pending"}`;
  renderFacts(analysis);
  renderPriorities(analysis);
  renderPractice(analysis);
  renderQuestions(analysis);
  renderDownloads();
}

async function getAnalysis(jobId) {
  state.jobId = jobId;
  setStatus("Loading source-linked question evidence…");
  try {
    const analysis = await fetchJson(`/api/v1/history/${encodeURIComponent(jobId)}/analysis`);
    renderAnalysis(analysis);
    setStatus("Local question evidence loaded. Prepare it locally or consent to GPT analysis.");
  } catch (error) { setStatus(error instanceof Error ? error.message : "Analysis could not be loaded.", true); }
}

async function saveAnalysis(consent) {
  if (!state.jobId || state.loading) return;
  state.loading = true;
  el.local.disabled = true; el.cloud.disabled = true;
  setStatus(consent ? "Sending consented structured question evidence for GPT analysis…" : "Saving local question evidence…");
  try {
    const analysis = await fetchJson(`/api/v1/history/${encodeURIComponent(state.jobId)}/analysis`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subject: el.subject.value || null, academic_level: el.level.value || null, topic_taxonomy: taxonomy(), consent_to_cloud: consent }),
    });
    renderAnalysis(analysis);
    setStatus(consent ? "GPT suggestions are ready for teacher review." : "Local question map saved. OCR evidence has not left this computer.");
  } catch (error) { setStatus(error instanceof Error ? error.message : "Analysis could not be saved.", true); }
  finally { state.loading = false; el.local.disabled = false; el.cloud.disabled = !el.consent.checked; }
}

async function generatePractice() {
  if (!state.jobId || state.loading) return;
  el.practiceGenerate.disabled = true;
  setStatus("Generating local, teacher-reviewable practice starters…");
  try {
    const practice = await fetchJson(`/api/v1/history/${encodeURIComponent(state.jobId)}/analysis/practice`, { method: "POST" });
    if (state.analysis) {
      state.analysis.practice_items = practice.items || [];
      renderPractice(state.analysis);
    }
    setStatus("Local practice starters are ready for teacher review. No OCR file or source image was sent.");
  } catch (error) { setStatus(error instanceof Error ? error.message : "Practice starters could not be generated.", true); }
  finally { el.practiceGenerate.disabled = false; }
}

async function loadHistory() {
  try {
    const payload = await fetchJson("/api/v1/history?limit=100&status=completed&sort=created_at&order=desc");
    clearNode(el.history);
    payload.items.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.job_id;
      option.textContent = `${item.input_name} · ${item.document_profile || "document"}`;
      el.history.append(option);
    });
    const requested = new URLSearchParams(window.location.search).get("job");
    if (requested && Array.from(el.history.options).some((option) => option.value === requested)) {
      el.history.value = requested;
      await getAnalysis(requested);
    }
  } catch (error) { setStatus(error instanceof Error ? error.message : "History could not be loaded.", true); }
}

el.history.addEventListener("change", () => { const id = selectedIds()[0]; if (id) getAnalysis(id); });
el.consent.addEventListener("change", () => { el.cloud.disabled = !el.consent.checked || state.loading; });
el.local.addEventListener("click", () => saveAnalysis(false));
el.practiceGenerate.addEventListener("click", generatePractice);
el.cloud.addEventListener("click", () => saveAnalysis(true));
el.compare.addEventListener("click", async () => {
  const ids = selectedIds();
  if (ids.length < 2) { setStatus("Select at least two saved analyses to compare recurring topics.", true); return; }
  try {
    const result = await fetchJson("/api/v1/analysis/aggregate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_ids: ids }) });
    if (state.analysis) { state.analysis.analysis.recurring_topics = result.recurring_topics; renderPriorities(state.analysis); }
    setStatus("Selected analyses compared locally.");
  } catch (error) { setStatus(error instanceof Error ? error.message : "Selected analyses could not be compared.", true); }
});

loadHistory();

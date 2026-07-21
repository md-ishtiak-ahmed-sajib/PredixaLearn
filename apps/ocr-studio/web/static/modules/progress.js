// @ts-check

const PIPELINE_STEPS = ["upload", "prepare", "recognize", "structure", "verify", "export"];
const STAGE_TO_STEP = {
  uploading: "upload", queued: "prepare", initializing: "prepare", pdf_inspection: "prepare", source_preview: "prepare",
  pdf_rendering: "prepare", text_recognition: "recognize", layout_analysis: "recognize",
  vl_processing: "recognize", page_complete: "recognize", finalizing: "recognize",
  structure_analysis: "structure", table_analysis: "structure", table_extraction: "structure",
  figure_detection: "structure", cropping: "structure", quality_validation: "verify",
  adaptive_retry: "verify", correction: "verify", markdown_cleanup: "export",
  docx_generation: "export", libreoffice_validation: "export", archiving: "export",
  publishing: "export", completed: "export",
};

/**
 * @param {{ elements: Record<string, any>, state: Record<string, any> }} context
 */
export function createProgressRenderer({ elements, state }) {
  function addActivity(progress) {
    const key = [progress.stage, progress.message, progress.current_page, progress.completed_pages].join("|");
    if (state.activityKeys.has(key)) return;
    state.activityKeys.add(key);
    const item = document.createElement("li");
    item.textContent = progress.message;
    elements.activityLog.append(item);
    elements.activityLog.scrollTop = elements.activityLog.scrollHeight;
  }

  function renderPipeline(activeStep, status) {
    const activeIndex = PIPELINE_STEPS.indexOf(activeStep);
    elements.pipelineSteps.forEach((step) => {
      const stepIndex = PIPELINE_STEPS.indexOf(step.dataset.step);
      const active = status !== "completed" && stepIndex === activeIndex;
      step.classList.toggle("done", status === "completed" || stepIndex < activeIndex);
      step.classList.toggle("active", active);
      if (active) step.setAttribute("aria-current", "step");
      else step.removeAttribute("aria-current");
    });
  }

  function renderPages(progress) {
    const total = Number(progress.total_pages);
    if (!Number.isInteger(total) || total < 1) {
      elements.pageProgress.classList.add("hidden");
      return;
    }
    const completed = Math.min(Math.max(Number(progress.completed_pages) || 0, 0), total);
    const current = Math.min(Math.max(Number(progress.current_page) || 0, 0), total);
    elements.pageProgress.classList.remove("hidden");
    elements.pageSummary.textContent = `${completed} of ${total} complete`;
    elements.pageProgressBar.max = total;
    elements.pageProgressBar.value = completed;
    elements.pageProgressBar.textContent = `${completed} of ${total} pages`;
    if (elements.pageGrid.children.length !== total) {
      elements.pageGrid.replaceChildren();
      for (let pageNumber = 1; pageNumber <= total; pageNumber += 1) {
        const chip = document.createElement("span");
        chip.className = "page-chip";
        chip.dataset.page = String(pageNumber);
        chip.textContent = String(pageNumber);
        elements.pageGrid.append(chip);
      }
    }
    Array.from(elements.pageGrid.children).forEach((chip) => {
      const pageNumber = Number(chip.dataset.page);
      chip.classList.toggle("done", pageNumber <= completed);
      chip.classList.toggle("active", pageNumber === current && pageNumber > completed);
      const pageStatus = pageNumber <= completed ? "complete" : pageNumber === current ? "processing" : "waiting";
      chip.setAttribute("aria-label", `Page ${pageNumber}: ${pageStatus}`);
    });
  }

  function renderProgress(progress, status = "processing") {
    const activeStep = STAGE_TO_STEP[progress.stage] || state.activeStep;
    state.activeStep = activeStep;
    elements.progress.classList.remove("hidden");
    elements.progressText.textContent = progress.message;
    elements.progress.classList.toggle("failed", status === "failed" || status === "cancelled");
    const terminal = ["completed", "failed", "cancelled"].includes(status);
    elements.progressSpinner.classList.toggle("hidden", terminal);
    elements.cancelJob.classList.toggle("hidden", terminal);
    elements.cancelJob.disabled = Boolean(progress.cancel_requested);
    renderPipeline(activeStep, status);
    renderPages(progress);
    addActivity(progress);
  }

  function resetVisualization() {
    state.activityKeys.clear();
    state.activeStep = "upload";
    elements.activityLog.replaceChildren();
    elements.pageGrid.replaceChildren();
    elements.pageProgress.classList.add("hidden");
    elements.progress.classList.remove("failed");
    elements.progressSpinner.classList.remove("hidden");
    elements.pipelineSteps.forEach((step) => {
      step.classList.remove("active", "done");
      step.removeAttribute("aria-current");
    });
    elements.resultActions.classList.add("hidden");
    elements.resultView.classList.add("hidden");
    elements.tableDownloads.replaceChildren();
  }

  return { renderProgress, resetVisualization };
}

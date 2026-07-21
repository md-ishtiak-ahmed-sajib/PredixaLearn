// @ts-check

const ACTIVE_JOB_KEY = "predixalearn.activeJob.v1";

/** @param {{ job_id: string, input_name: string, workflow: string }} job */
export function saveActiveJob(job) {
  localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify({
    ...job,
    saved_at: new Date().toISOString(),
  }));
}

/** @returns {{ job_id: string, input_name?: string, workflow?: string } | null} */
export function loadActiveJob() {
  try {
    const value = JSON.parse(localStorage.getItem(ACTIVE_JOB_KEY) || "null");
    return typeof value?.job_id === "string" ? value : null;
  } catch {
    clearActiveJob();
    return null;
  }
}

export function clearActiveJob() {
  localStorage.removeItem(ACTIVE_JOB_KEY);
}

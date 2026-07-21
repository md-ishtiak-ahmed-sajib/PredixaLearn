// @ts-check

/** @param {string | null | undefined} value */
export function formatHistoryDate(value) {
  if (!value) return "Not available";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

/** @param {number | null | undefined} milliseconds */
export function formatHistoryDuration(milliseconds) {
  if (!Number.isFinite(milliseconds)) return "Not available";
  const seconds = Math.max(0, Math.round(Number(milliseconds) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

/** @ts-check */

/**
 * Fetch JSON from the local service and retain the HTTP status on failures.
 * @param {string} url
 * @param {RequestInit} [options]
 * @returns {Promise<any>}
 */
export async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { detail: "The service returned an invalid response." };
  }
  if (!response.ok) {
    const error = /** @type {Error & {status?: number}} */ (
      new Error(payload.detail || `Request failed (${response.status})`)
    );
    error.status = response.status;
    throw error;
  }
  return payload;
}

/** @param {number} milliseconds */
export function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

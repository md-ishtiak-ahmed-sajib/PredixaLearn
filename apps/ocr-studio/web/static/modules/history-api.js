// @ts-check

/** @param {string} url @param {RequestInit} [options] */
export async function historyFetch(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { detail: "The service returned an invalid response." };
  }
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return payload;
}

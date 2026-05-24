/**
 * Genesis standalone API client.
 *
 * Simplified version of Agent Zero's api.js.
 * No CSRF token exchange — Genesis standalone runs on localhost
 * without authentication.  Plain fetch passthrough.
 */

// Exponential backoff state for server error recovery.
// Prevents dashboard from flooding a recovering server.
let _consecutiveFailures = 0;
const _BASE_DELAY_MS = 1000;
const _MAX_DELAY_MS = 60000;
const _MAX_JITTER_MS = 2000;

/**
 * Call a JSON-in JSON-out API endpoint.
 * @param {string} endpoint - The API endpoint to call
 * @param {any} data - The data to send to the API
 * @returns {Promise<any>} The JSON response from the API
 */
export async function callJsonApi(endpoint, data) {
  const response = await fetchApi(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(error);
  }
  return await response.json();
}

/**
 * Fetch wrapper for Genesis APIs.
 * Redirects to login on 401 (expired/missing session).
 * Applies exponential backoff on server errors (5xx / network failure).
 * @param {string} url - The URL to fetch
 * @param {Object} [request] - The fetch request options
 * @returns {Promise<Response>} The fetch response
 */
export async function fetchApi(url, request) {
  // Backoff gate: delay before fetch if server was recently failing
  if (_consecutiveFailures > 0) {
    const delay = Math.min(
      _BASE_DELAY_MS * Math.pow(2, _consecutiveFailures - 1),
      _MAX_DELAY_MS,
    ) + Math.random() * _MAX_JITTER_MS;
    await new Promise((r) => setTimeout(r, delay));
  }

  try {
    const resp = await fetch(url, request || {});
    if (resp.status === 401 && !url.includes("/auth/")) {
      window.location.href = "/genesis/login";
    }
    if (resp.ok) {
      _consecutiveFailures = 0;
    } else if (resp.status >= 500) {
      _consecutiveFailures++;
    }
    return resp;
  } catch (e) {
    _consecutiveFailures++;
    throw e;
  }
}

/** Current backoff failure count (for testing / dashboard display). */
export function getBackoffFailures() {
  return _consecutiveFailures;
}

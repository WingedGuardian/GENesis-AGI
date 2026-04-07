/**
 * Genesis standalone API client.
 *
 * Simplified version of Agent Zero's api.js.
 * No CSRF token exchange — Genesis standalone runs on localhost
 * without authentication.  Plain fetch passthrough.
 */

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
 * @param {string} url - The URL to fetch
 * @param {Object} [request] - The fetch request options
 * @returns {Promise<Response>} The fetch response
 */
export async function fetchApi(url, request) {
  const resp = await fetch(url, request || {});
  if (resp.status === 401 && !url.includes("/auth/")) {
    window.location.href = "/genesis/login";
  }
  return resp;
}

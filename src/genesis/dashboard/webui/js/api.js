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
 * @param {string} url - The URL to fetch
 * @param {Object} [request] - The fetch request options
 * @returns {Promise<Response>} The fetch response
 */
export async function fetchApi(url, request) {
  return await fetch(url, request || {});
}

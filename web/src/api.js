// Every call to the backend lives here, so no screen has to know about URLs,
// status codes, or how FastAPI shapes its errors.

const API_BASE_URL = "http://127.0.0.1:8000";

/** The backend answered, but said no. `message` is safe to show a user. */
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The request never got an answer: the backend is down or unreachable. */
export class NetworkError extends Error {
  constructor() {
    super(
      `Could not reach the backend at ${API_BASE_URL}. ` +
        "Start it with: uvicorn --app-dir src shortcut.api:app --reload"
    );
    this.name = "NetworkError";
  }
}

/**
 * Pull a readable message out of an error response.
 *
 * A 404 sends {"detail": "..."}, but FastAPI's own validation errors (422)
 * send {"detail": [{...}, ...]}, which would show as "[object Object]".
 */
function messageFrom(body, status) {
  const detail = body?.detail;

  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((problem) => problem.msg ?? "Invalid value").join("; ");
  }
  return `The server replied with status ${status}.`;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, options);
  } catch {
    throw new NetworkError();
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // Some replies have no body at all; that is only a problem if it failed.
  }

  if (!response.ok) {
    throw new ApiError(messageFrom(body, response.status), response.status);
  }
  return body;
}

function postJson(path, payload) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const fetchNodes = () => request("/nodes");
export const fetchEdges = () => request("/edges");
export const requestRoute = (payload) => postJson("/route", payload);
export const submitReport = (payload) => postJson("/reports", payload);
export const fetchReportGroups = () => request("/reports/groups");

/** Approve or reject one group of reports. `action` is "approve" or "reject". */
export const reviewReportGroup = (key, action) =>
  postJson(`/reports/groups/${encodeURIComponent(key)}/${action}`, {});

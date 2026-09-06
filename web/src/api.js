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

/** The same route, plus other ways round worth considering. */
export const requestRouteOptions = (payload) => postJson("/route/options", payload);

// --- the plain-language box ------------------------------------------------

/**
 * Read a typed sentence into a route request.
 *
 * `current` is what the controls already show, and is only read for the
 * preferences - the backend overwrites its origin and destination with
 * whatever the sentence resolved to. It is sent only when both places are
 * already chosen, because a RouteRequest cannot be built without them.
 *
 * The answer is not always a route: an ambiguous place ("staircase 1", which
 * exists on two floors) comes back with `question` and the candidates it
 * could have meant, and no request at all.
 */
export const parseSentence = (text, current = null) =>
  postJson("/ai/parse", current ? { text, current } : { text });

/** Whether the plain-language box is worth showing at all.
 *
 * The /ai routes are only mounted when the AI dependencies are installed, so
 * a machine without them 404s here rather than erroring - which is the same
 * answer as "not available", and is why this never throws.
 */
export async function aiAvailable() {
  try {
    const health = await request("/ai/health");
    return Boolean(health?.available);
  } catch {
    return false;
  }
}

/** The floorplan for one floor, or null when nobody has uploaded one yet. */
export async function fetchFloorplan(building, floor) {
  const query = new URLSearchParams({ building, floor });
  const found = await request(`/floorplans?${query}`);
  return Array.isArray(found) && found.length > 0 ? found[0] : null;
}
export const submitReport = (payload) => postJson("/reports", payload);
export const fetchReportGroups = () => request("/reports/groups");

/** Everything sitting in the overrides file, not yet folded into the survey. */
export const fetchPendingChanges = () => request("/admin/pending");

// --- reading a drawing into changes to review -----------------------------
//
// Nothing here is on the map. A candidate routes nobody and appears in no
// search until it is approved, which is the moment it moves into the
// overrides file and starts behaving like a place added by hand.

/** Upload one or more node-map PDFs and queue whatever the map lacks. */
export function importDrawings(files) {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  return request("/admin/import", { method: "POST", body: form });
}

export const fetchImportCandidates = () => request("/admin/import/candidates");

/** Correct a candidate before approving it. Only send what changed. */
export const updateImportCandidate = (id, changes) =>
  patchJson(`/admin/import/candidates/${encodeURIComponent(id)}`, changes);

export const approveImportCandidate = (id) =>
  postJson(`/admin/import/candidates/${encodeURIComponent(id)}/approve`, {});

export const approveAllImportCandidates = () =>
  postJson("/admin/import/approve-all", {});

export const rejectImportCandidate = (id) =>
  request(`/admin/import/candidates/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });

/** Approve or reject one group of reports. `action` is "approve" or "reject". */
export const reviewReportGroup = (key, action) =>
  postJson(`/reports/groups/${encodeURIComponent(key)}/${action}`, {});

// --- editing the map, and photos of it ------------------------------------

export const addNode = (payload) => postJson("/admin/nodes", payload);
export const addEdge = (payload) => postJson("/admin/edges", payload);

const patchJson = (path, payload) =>
  request(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

export const updateNode = (nodeId, changes) =>
  patchJson(`/admin/nodes/${encodeURIComponent(nodeId)}`, changes);
export const updateEdge = (edgeId, changes) =>
  patchJson(`/admin/edges/${encodeURIComponent(edgeId)}`, changes);

/**
 * Remove a place or a link, whether it was added here or surveyed.
 *
 * Removing a place removes every link to it: an edge whose end does not exist
 * makes the map refuse to build, so that cascade is what keeps it loadable
 * rather than a convenience. Nothing reaches the survey file either way -
 * a surveyed deletion is recorded as pending until somebody graduates it.
 */
export const deleteTarget = (targetKind, targetId) =>
  request(
    `/admin/${targetKind === "node" ? "nodes" : "edges"}/${encodeURIComponent(targetId)}`,
    { method: "DELETE" }
  );

export const fetchPhotos = (targetKind, targetId) => {
  const query = new URLSearchParams({
    target_kind: targetKind,
    target_id: targetId,
  });
  return request(`/photos?${query}`);
};

/**
 * Upload one photo.
 *
 * Sent as multipart form data rather than JSON, because it carries a file.
 * The Content-Type header is left unset on purpose: the browser has to add it
 * itself, complete with the boundary marker separating the parts.
 */
export function uploadPhoto({ file, targetKind, targetId, facing, location, caption }) {
  const form = new FormData();
  form.append("file", file);
  form.append("target_kind", targetKind);
  form.append("target_id", targetId);
  if (facing) form.append("facing", facing);
  if (location) form.append("location", location);
  if (caption) form.append("caption", caption);

  return request("/photos", { method: "POST", body: form });
}

export const deletePhoto = (photoId) =>
  request(`/photos/${encodeURIComponent(photoId)}`, { method: "DELETE" });

/**
 * Change what is recorded about a photo, above all which way it faces.
 *
 * A bulk upload cannot know the direction - the files are named by number and
 * timestamp - so photos arrive undirected and get labelled afterwards by
 * somebody who recognises the corridor. Clearing a direction needs its own
 * flag, because a null `facing` means "leave it alone".
 */
export const updatePhoto = (photoId, changes) =>
  patchJson(`/photos/${encodeURIComponent(photoId)}`, changes);

/** Turn a photo's path into something an <img> can load. */
export const photoUrl = (path) => `${API_BASE_URL}${path}`;

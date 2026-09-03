// Shortcut frontend.
//
// On load, it fetches the list of navigation points from the backend's
// GET /nodes and uses that to build the two dropdowns — nothing about the
// building is hardcoded here, so the page can never drift out of sync with
// data/campus_graph.json the way a hand-typed list could.
//
// Then it does what it always did: collect two node ids, POST them to
// /route, and show the answer. All the routing work happens on the backend;
// this file only sends requests and draws results. No AI, no map drawing,
// no libraries.

import "./style.css";

// Where the backend is listening. The backend must allow this page's address
// (http://localhost:5173) in its CORS settings, which it does for development.
const API_BASE_URL = "http://127.0.0.1:8000";

// Default picks, used if they turn out to exist in the fetched node list.
const DEFAULT_ORIGIN = "Hive_B5_A";
const DEFAULT_DESTINATION = "Hive_B4_E";

// --------------------------------------------------------------------------
// The page elements we need
// --------------------------------------------------------------------------

const form = document.querySelector("#route-form");
const originSelect = document.querySelector("#origin");
const destinationSelect = document.querySelector("#destination");
const findButton = document.querySelector("#find-button");

const allowStairsCheckbox = document.querySelector("#allow-stairs");
const allowLiftCheckbox = document.querySelector("#allow-lift");
const shelteredOnlyCheckbox = document.querySelector("#sheltered-only");

const loadingMessage = document.querySelector("#loading");
const errorMessage = document.querySelector("#error");

const resultCard = document.querySelector("#result");
const totalTimeOutput = document.querySelector("#total-time");
const totalDistanceOutput = document.querySelector("#total-distance");
const stairsBadge = document.querySelector("#badge-stairs");
const liftBadge = document.querySelector("#badge-lift");
const shelterBadge = document.querySelector("#badge-shelter");
const stepsList = document.querySelector("#route-steps");

// Filled in once GET /nodes succeeds. Maps a node id to its full
// { id, name, floor } record, so showRoute() can look up readable names.
let nodesById = new Map();

// --------------------------------------------------------------------------
// Showing one state at a time
// --------------------------------------------------------------------------

/** Hide the loading, error and result areas. */
function clearOutput() {
  loadingMessage.hidden = true;
  errorMessage.hidden = true;
  resultCard.hidden = true;
}

function showLoading(text) {
  clearOutput();
  loadingMessage.textContent = text;
  loadingMessage.hidden = false;
  findButton.disabled = true;
}

function stopLoading() {
  loadingMessage.hidden = true;
  findButton.disabled = false;
  findButton.textContent = "Find route";
}

function showError(text) {
  clearOutput();
  errorMessage.textContent = text;
  errorMessage.hidden = false;
}

// --------------------------------------------------------------------------
// Talking to the backend
// --------------------------------------------------------------------------

/**
 * Pull a readable message out of an error response.
 *
 * The backend sends {"detail": "..."} for a 404, but FastAPI's own validation
 * errors (422) send {"detail": [{...}, ...]}, which would print as
 * "[object Object]" if used directly.
 */
function describeErrorBody(body, status) {
  const detail = body?.detail;

  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((problem) => problem.msg ?? "Invalid value")
      .join("; ");
  }
  return `The server replied with status ${status}.`;
}

/** A network-level failure, shown the same way everywhere it can happen. */
function unreachableBackendMessage() {
  return (
    `Could not reach the backend at ${API_BASE_URL}. ` +
    "Start it with: uvicorn --app-dir src shortcut.api:app --reload"
  );
}

// --------------------------------------------------------------------------
// Loading the list of navigation points
// --------------------------------------------------------------------------

/** Build one dropdown from a list of nodes, grouped by building and floor. */
function fillDropdown(select, nodes, selectedId) {
  select.replaceChildren();

  // Group by building-and-floor, keeping the order those groups first appear
  // in, so the page reflects however data/campus_graph.json orders things.
  // Nothing about the building is hardcoded here.
  const groupOrder = [];
  const nodesByGroup = new Map();
  for (const node of nodes) {
    const key = `${node.building} · Level ${node.floor}`;
    if (!nodesByGroup.has(key)) {
      groupOrder.push(key);
      nodesByGroup.set(key, []);
    }
    nodesByGroup.get(key).push(node);
  }

  for (const key of groupOrder) {
    // Node names such as "Staircase 1" repeat on every floor, so the group
    // heading is what tells the user which one they are picking.
    const group = document.createElement("optgroup");
    group.label = key;

    for (const node of nodesByGroup.get(key)) {
      const option = document.createElement("option");
      option.value = node.id;
      // Include the building and floor in the option text itself, not just
      // the group heading: once a value is chosen, a collapsed <select> only
      // ever displays the selected option's own text, so "Staircase 1" alone
      // would be ambiguous the moment a second building has one too.
      option.textContent = `${node.name} (${node.building} · ${node.floor})`;
      option.selected = node.id === selectedId;
      group.appendChild(option);
    }

    select.appendChild(group);
  }
}

async function loadNodes() {
  showLoading("Loading locations…");

  let response;
  try {
    response = await fetch(`${API_BASE_URL}/nodes`);
  } catch {
    // Leave the button disabled: with no locations there is nothing to route
    // between, so re-enabling it would only produce a second error.
    showError(unreachableBackendMessage());
    return;
  }

  if (!response.ok) {
    showError(`Could not load the list of locations (status ${response.status}).`);
    return;
  }

  let nodes;
  try {
    nodes = await response.json();
  } catch {
    showError("The list of locations came back in a format the page could not read.");
    return;
  }

  if (!Array.isArray(nodes) || nodes.length === 0) {
    showError("The backend returned no locations, so no route can be planned.");
    return;
  }

  nodesById = new Map(nodes.map((node) => [node.id, node]));

  // Fall back to the first two nodes if the defaults are not in this graph.
  const originId = nodesById.has(DEFAULT_ORIGIN) ? DEFAULT_ORIGIN : nodes[0]?.id;
  const destinationId = nodesById.has(DEFAULT_DESTINATION)
    ? DEFAULT_DESTINATION
    : nodes[1]?.id;

  fillDropdown(originSelect, nodes, originId);
  fillDropdown(destinationSelect, nodes, destinationId);

  stopLoading();
}

// --------------------------------------------------------------------------
// Turning numbers into readable text
// --------------------------------------------------------------------------

/** 49 -> "49 sec". 95 -> "1 min 35 sec". */
function formatSeconds(totalSeconds) {
  const seconds = Math.round(totalSeconds);
  if (seconds < 60) {
    return `${seconds} sec`;
  }
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder === 0
    ? `${minutes} min`
    : `${minutes} min ${remainder} sec`;
}

/** 55.599999999999994 -> "55.6 m". Rounds away floating-point noise. */
function formatMetres(totalMetres) {
  return `${totalMetres.toFixed(1)} m`;
}

/** Fill one badge with a tick or a cross, plus a matching style. */
function setBadge(element, isTrue, trueText, falseText) {
  element.textContent = isTrue ? `✓ ${trueText}` : `✗ ${falseText}`;
  element.className = isTrue ? "badge yes" : "badge no";
}

/** Look up a node's readable name; falls back to the id if it is unknown. */
function nodeName(nodeId) {
  return nodesById.get(nodeId)?.name ?? nodeId;
}

// --------------------------------------------------------------------------
// Showing a route
// --------------------------------------------------------------------------

function showRoute(route) {
  clearOutput();

  totalTimeOutput.textContent = formatSeconds(route.total_walk_seconds);
  totalDistanceOutput.textContent = formatMetres(route.total_distance_m);

  setBadge(stairsBadge, route.uses_stairs, "Uses stairs", "No stairs");
  setBadge(liftBadge, route.uses_lift, "Uses lift", "No lift");
  setBadge(shelterBadge, route.fully_sheltered, "Sheltered", "Partly exposed");

  // Rebuild the step list from scratch each time.
  stepsList.replaceChildren();
  for (const nodeId of route.nodes) {
    const item = document.createElement("li");

    const name = document.createElement("span");
    name.className = "step-name";
    name.textContent = nodeName(nodeId);

    const id = document.createElement("span");
    id.className = "step-id";
    id.textContent = nodeId;

    item.append(name, id);
    stepsList.appendChild(item);
  }

  // A route to where you already are has one node and no steps.
  if (route.nodes.length === 1) {
    const note = document.createElement("li");
    note.className = "step-note";
    note.textContent = "You are already there.";
    stepsList.appendChild(note);
  }

  resultCard.hidden = false;
}

/** Read the preference controls into the shape POST /route expects. */
function currentOptions() {
  const chosen = form.querySelector('input[name="preference"]:checked');
  return {
    preference: chosen ? chosen.value : "fastest",
    allow_stairs: allowStairsCheckbox.checked,
    allow_lift: allowLiftCheckbox.checked,
    sheltered_only: shelteredOnlyCheckbox.checked,
  };
}

async function findRoute(origin, destination) {
  showLoading("Finding the best route…");
  findButton.textContent = "Finding…";

  let response;
  try {
    response = await fetch(`${API_BASE_URL}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin, destination, ...currentOptions() }),
    });
  } catch {
    // fetch only rejects when the request never got an answer: the backend is
    // not running, the address is wrong, or the browser blocked it.
    stopLoading();
    showError(unreachableBackendMessage());
    return;
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // A non-JSON reply, which should not happen but must not crash the page.
  }

  stopLoading();

  if (!response.ok) {
    showError(describeErrorBody(body, response.status));
    return;
  }

  showRoute(body);
}

// --------------------------------------------------------------------------
// Start
// --------------------------------------------------------------------------

loadNodes();

form.addEventListener("submit", (event) => {
  event.preventDefault(); // stay on the page instead of reloading
  findRoute(originSelect.value, destinationSelect.value);
});

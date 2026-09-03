// Shortcut frontend, first functional version.
//
// It does one thing: collect two node ids from the dropdowns, POST them to the
// FastAPI backend's /route endpoint, and show the answer. All the routing work
// happens on the backend; this file only sends the request and draws the
// result. No AI, no map drawing, no libraries.

import { FLOORS, NODES, nodeName } from "./nodes.js";
import "./style.css";

// Where the backend is listening. The backend must allow this page's address
// (http://localhost:5173) in its CORS settings, which it does for development.
const API_BASE_URL = "http://127.0.0.1:8000";

// Default picks, so the page is usable the moment it loads.
const DEFAULT_ORIGIN = "Hive_B5_A";
const DEFAULT_DESTINATION = "Hive_B4_E";

// --------------------------------------------------------------------------
// The page elements we need
// --------------------------------------------------------------------------

const form = document.querySelector("#route-form");
const originSelect = document.querySelector("#origin");
const destinationSelect = document.querySelector("#destination");
const findButton = document.querySelector("#find-button");

const loadingMessage = document.querySelector("#loading");
const errorMessage = document.querySelector("#error");

const resultCard = document.querySelector("#result");
const totalTimeOutput = document.querySelector("#total-time");
const totalDistanceOutput = document.querySelector("#total-distance");
const stairsBadge = document.querySelector("#badge-stairs");
const liftBadge = document.querySelector("#badge-lift");
const shelterBadge = document.querySelector("#badge-shelter");
const stepsList = document.querySelector("#route-steps");

// --------------------------------------------------------------------------
// Filling the dropdowns
// --------------------------------------------------------------------------

/** Put every node into one dropdown, grouped by floor. */
function fillDropdown(select, selectedId) {
  for (const floor of FLOORS) {
    const group = document.createElement("optgroup");
    group.label = `Level ${floor}`;

    for (const node of NODES.filter((candidate) => candidate.floor === floor)) {
      const option = document.createElement("option");
      option.value = node.id;
      option.textContent = node.name;
      option.selected = node.id === selectedId;
      group.appendChild(option);
    }

    select.appendChild(group);
  }
}

// --------------------------------------------------------------------------
// Showing one state at a time
// --------------------------------------------------------------------------

/** Hide the loading, error and result areas. */
function clearOutput() {
  loadingMessage.hidden = true;
  errorMessage.hidden = true;
  resultCard.hidden = true;
}

function showLoading() {
  clearOutput();
  loadingMessage.hidden = false;
  findButton.disabled = true;
  findButton.textContent = "Finding…";
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

async function findRoute(origin, destination) {
  showLoading();

  let response;
  try {
    response = await fetch(`${API_BASE_URL}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ origin, destination }),
    });
  } catch (networkProblem) {
    // fetch only rejects when the request never got an answer: the backend is
    // not running, the address is wrong, or the browser blocked it.
    stopLoading();
    showError(
      `Could not reach the backend at ${API_BASE_URL}. ` +
        "Start it with: uvicorn --app-dir src shortcut.api:app --reload"
    );
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

fillDropdown(originSelect, DEFAULT_ORIGIN);
fillDropdown(destinationSelect, DEFAULT_DESTINATION);

form.addEventListener("submit", (event) => {
  event.preventDefault(); // stay on the page instead of reloading
  findRoute(originSelect.value, destinationSelect.value);
});

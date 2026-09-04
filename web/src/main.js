// Shortcut frontend.
//
// On load it fetches the list of places from the backend's GET /nodes, so
// nothing about the building is hardcoded here and the page cannot drift out
// of sync with data/campus_graph.json.
//
// Then: pick a start and an end from the search boxes, POST them to /route,
// and walk through the answer one step at a time. All the routing happens on
// the backend. No AI, no map drawing, no libraries.

import "./style.css";

// Where the backend is listening. The backend must allow this page's address
// (http://localhost:5173) in its CORS settings, which it does for development.
const API_BASE_URL = "http://127.0.0.1:8000";

// Places to offer before the user has typed anything, and the most to offer
// at once. Small enough to scan, long enough to be useful.
const MAX_SUGGESTIONS = 8;

// --------------------------------------------------------------------------
// The page elements we need
// --------------------------------------------------------------------------

const form = document.querySelector("#route-form");
const originInput = document.querySelector("#origin-input");
const originSuggestions = document.querySelector("#origin-suggestions");
const destinationInput = document.querySelector("#destination-input");
const destinationSuggestions = document.querySelector("#destination-suggestions");
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
const stepProgress = document.querySelector("#step-progress");
const backButton = document.querySelector("#back-button");
const nextButton = document.querySelector("#next-button");

// Every place the backend knows about, and a lookup by id.
let allNodes = [];
let nodesById = new Map();

// The route currently on screen, and how far through it the user says they are.
let currentSteps = [];
let currentStepIndex = 0;

// --------------------------------------------------------------------------
// Showing one state at a time
// --------------------------------------------------------------------------

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
    return detail.map((problem) => problem.msg ?? "Invalid value").join("; ");
  }
  return `The server replied with status ${status}.`;
}

function unreachableBackendMessage() {
  return (
    `Could not reach the backend at ${API_BASE_URL}. ` +
    "Start it with: uvicorn --app-dir src shortcut.api:app --reload"
  );
}

// --------------------------------------------------------------------------
// Search boxes
// --------------------------------------------------------------------------

/** How well a place matches what has been typed. Higher is better, -1 is no match. */
function matchScore(node, query) {
  if (!query) {
    return 0; // nothing typed yet: everything is equally worth showing
  }

  const name = node.name.toLowerCase();
  const everything =
    `${node.name} ${node.building} ${node.floor} ${node.id}`.toLowerCase();

  if (name.startsWith(query)) return 3;
  if (name.includes(query)) return 2;
  if (everything.includes(query)) return 1;
  return -1;
}

/** The best few places for what has been typed so far. */
function suggestionsFor(query) {
  const cleaned = query.trim().toLowerCase();

  return allNodes
    .map((node) => ({ node, score: matchScore(node, cleaned) }))
    .filter((entry) => entry.score >= 0)
    .sort((a, b) => b.score - a.score) // Array.sort is stable, so ties keep graph order
    .slice(0, MAX_SUGGESTIONS)
    .map((entry) => entry.node);
}

/** The text shown in the box once a place is chosen. */
function labelFor(node) {
  return `${node.name} (${node.building} · ${node.floor})`;
}

/**
 * Wire up one search box.
 *
 * Returns a small object with `selectedId()`, because the box's text alone is
 * not a valid answer: the backend needs a node id, and what the user typed may
 * match nothing at all.
 */
function createSearchBox(input, list) {
  let selectedId = null;
  let highlighted = -1;
  let shown = [];

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    highlighted = -1;
  }

  function choose(node) {
    selectedId = node.id;
    input.value = labelFor(node);
    close();
  }

  function render(nodes) {
    shown = nodes;
    list.replaceChildren();

    if (nodes.length === 0) {
      const empty = document.createElement("li");
      empty.className = "suggestion-empty";
      empty.textContent = "No matching place";
      list.appendChild(empty);
    }

    nodes.forEach((node, index) => {
      const item = document.createElement("li");
      item.className = "suggestion";
      item.role = "option";
      item.dataset.index = String(index);

      const name = document.createElement("span");
      name.className = "suggestion-name";
      name.textContent = node.name;

      const where = document.createElement("span");
      where.className = "suggestion-where";
      where.textContent = `${node.building} · Level ${node.floor}`;

      item.append(name, where);
      // mousedown, not click: it fires before the input loses focus, so the
      // blur handler below cannot close the list out from under the tap.
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        choose(node);
      });
      list.appendChild(item);
    });

    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function highlight(next) {
    if (shown.length === 0) return;
    highlighted = (next + shown.length) % shown.length;
    for (const item of list.querySelectorAll(".suggestion")) {
      item.classList.toggle(
        "is-highlighted",
        Number(item.dataset.index) === highlighted
      );
    }
  }

  input.addEventListener("input", () => {
    // Typing after choosing means the choice no longer matches the text, so
    // it has to be given up: otherwise the box could read one place while
    // silently routing to another.
    selectedId = null;
    render(suggestionsFor(input.value));
  });

  input.addEventListener("focus", () => render(suggestionsFor(input.value)));

  input.addEventListener("blur", () => close());

  input.addEventListener("keydown", (event) => {
    if (list.hidden && event.key === "ArrowDown") {
      render(suggestionsFor(input.value));
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(highlighted + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(highlighted - 1);
    } else if (event.key === "Enter") {
      // Only swallow Enter when it is picking from the list; otherwise let it
      // submit the form as usual.
      if (!list.hidden && highlighted >= 0 && shown[highlighted]) {
        event.preventDefault();
        choose(shown[highlighted]);
      }
    } else if (event.key === "Escape") {
      close();
    }
  });

  return {
    selectedId: () => selectedId,
    typedText: () => input.value.trim(),
    setNode: (node) => choose(node),
  };
}

const originBox = createSearchBox(originInput, originSuggestions);
const destinationBox = createSearchBox(destinationInput, destinationSuggestions);

// --------------------------------------------------------------------------
// Loading the list of places
// --------------------------------------------------------------------------

async function loadNodes() {
  showLoading("Loading locations…");

  let response;
  try {
    response = await fetch(`${API_BASE_URL}/nodes`);
  } catch {
    // Leave the button disabled: with no places there is nothing to route
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

  allNodes = nodes;
  nodesById = new Map(nodes.map((node) => [node.id, node]));

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
  return remainder === 0 ? `${minutes} min` : `${minutes} min ${remainder} sec`;
}

/** 55.599999999999994 -> "55.6 m". Rounds away floating-point noise. */
function formatMetres(totalMetres) {
  return `${totalMetres.toFixed(1)} m`;
}

function setBadge(element, isTrue, trueText, falseText) {
  element.textContent = isTrue ? `✓ ${trueText}` : `✗ ${falseText}`;
  element.className = isTrue ? "badge yes" : "badge no";
}

function nodeName(nodeId) {
  return nodesById.get(nodeId)?.name ?? nodeId;
}

// --------------------------------------------------------------------------
// Walking through the steps
// --------------------------------------------------------------------------

/** Draw the step list, expanding whichever step the user is on. */
function renderSteps() {
  stepsList.replaceChildren();

  currentSteps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = "step";
    if (index === currentStepIndex) item.classList.add("is-current");
    if (index < currentStepIndex) item.classList.add("is-done");

    const heading = document.createElement("p");
    heading.className = "step-instruction";
    heading.textContent = step.instruction;
    item.appendChild(heading);

    // Only the step being walked shows its full description, so the list
    // stays scannable.
    if (index === currentStepIndex) {
      const detail = document.createElement("p");
      detail.className = "step-detail";
      detail.textContent = step.detail;
      item.appendChild(detail);

      const meta = document.createElement("p");
      meta.className = "step-meta";
      meta.textContent = `${formatMetres(step.distance_m)} · ${formatSeconds(
        step.walk_seconds
      )} · arrives at ${nodeName(step.to_id)}`;
      item.appendChild(meta);
    }

    stepsList.appendChild(item);
  });

  const arrived = currentStepIndex >= currentSteps.length;
  if (arrived) {
    const done = document.createElement("li");
    done.className = "step is-current step-arrived";
    done.textContent = "You have arrived.";
    stepsList.appendChild(done);
  }

  stepProgress.textContent = arrived
    ? "Journey complete"
    : `Step ${currentStepIndex + 1} of ${currentSteps.length}`;

  backButton.disabled = currentStepIndex === 0;
  nextButton.disabled = arrived;
  nextButton.textContent = currentStepIndex === currentSteps.length - 1
    ? "I'm here"
    : "Next step";
}

function showRoute(route) {
  clearOutput();

  totalTimeOutput.textContent = formatSeconds(route.total_walk_seconds);
  totalDistanceOutput.textContent = formatMetres(route.total_distance_m);

  setBadge(stairsBadge, route.uses_stairs, "Uses stairs", "No stairs");
  setBadge(liftBadge, route.uses_lift, "Uses lift", "No lift");
  setBadge(shelterBadge, route.fully_sheltered, "Sheltered", "Partly exposed");

  currentSteps = route.steps ?? [];
  currentStepIndex = 0;

  if (currentSteps.length === 0) {
    // Origin and destination are the same place.
    stepsList.replaceChildren();
    const note = document.createElement("li");
    note.className = "step step-arrived";
    note.textContent = "You are already there.";
    stepsList.appendChild(note);
    stepProgress.textContent = "";
    backButton.disabled = true;
    nextButton.disabled = true;
  } else {
    renderSteps();
  }

  resultCard.hidden = false;
}

backButton.addEventListener("click", () => {
  if (currentStepIndex > 0) {
    currentStepIndex -= 1;
    renderSteps();
  }
});

nextButton.addEventListener("click", () => {
  if (currentStepIndex < currentSteps.length) {
    currentStepIndex += 1;
    renderSteps();
  }
});

// --------------------------------------------------------------------------
// Asking for a route
// --------------------------------------------------------------------------

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

  const origin = originBox.selectedId();
  const destination = destinationBox.selectedId();

  // A typed-but-unchosen box is the common mistake here, and sending its raw
  // text would come back as a confusing "unknown node id" from the backend.
  if (!origin || !destination) {
    const missing = !origin ? "start" : "destination";
    showError(`Pick a ${missing} from the suggestions list.`);
    return;
  }

  findRoute(origin, destination);
});

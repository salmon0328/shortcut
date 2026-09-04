// The route planner: pick two places, ask the backend, then walk the answer
// one step at a time.

import { photoUrl, requestRoute, requestRouteOptions } from "./api.js";
import { nodeName } from "./data.js";
import { createSearchBox } from "./searchBox.js";
import { showEmptyMap, showRouteOnMap } from "./mapView.js";

const form = document.querySelector("#route-form");
const findButton = document.querySelector("#find-button");
const loadingMessage = document.querySelector("#loading");
const errorMessage = document.querySelector("#error");

const allowStairsCheckbox = document.querySelector("#allow-stairs");
const allowLiftCheckbox = document.querySelector("#allow-lift");
const allowShuttleCheckbox = document.querySelector("#allow-shuttle");
const shelteredOnlyCheckbox = document.querySelector("#sheltered-only");

const resultCard = document.querySelector("#result");
const totalTimeOutput = document.querySelector("#total-time");
const totalDistanceOutput = document.querySelector("#total-distance");
const stairsBadge = document.querySelector("#badge-stairs");
const liftBadge = document.querySelector("#badge-lift");
const shuttleBadge = document.querySelector("#badge-shuttle");
const shelterBadge = document.querySelector("#badge-shelter");
const totalExtra = document.querySelector("#total-extra");
const showOptionsButton = document.querySelector("#show-options");
const optionsList = document.querySelector("#route-options");
const stepsList = document.querySelector("#route-steps");
const stepProgress = document.querySelector("#step-progress");
const backButton = document.querySelector("#back-button");
const nextButton = document.querySelector("#next-button");

const originBox = createSearchBox(
  document.querySelector("#origin-input"),
  document.querySelector("#origin-suggestions")
);
const destinationBox = createSearchBox(
  document.querySelector("#destination-input"),
  document.querySelector("#destination-suggestions")
);

// The route on screen, and how far along the user says they are.
let currentSteps = [];
let currentStepIndex = 0;
// What was asked for, kept so "other routes" can ask the same thing again.
let lastRequest = null;

// --------------------------------------------------------------------------
// Showing one state at a time
// --------------------------------------------------------------------------

function clearOutput() {
  loadingMessage.hidden = true;
  errorMessage.hidden = true;
  resultCard.hidden = true;
}

export function showPlanLoading(text) {
  clearOutput();
  loadingMessage.textContent = text;
  loadingMessage.hidden = false;
  findButton.disabled = true;
}

export function stopPlanLoading() {
  loadingMessage.hidden = true;
  findButton.disabled = false;
  findButton.textContent = "Find route";
}

export function showPlanError(text) {
  clearOutput();
  errorMessage.textContent = text;
  errorMessage.hidden = false;
}

// --------------------------------------------------------------------------
// Readable numbers
// --------------------------------------------------------------------------

/** 49 -> "49 sec". 95 -> "1 min 35 sec". */
function formatSeconds(totalSeconds) {
  const seconds = Math.round(totalSeconds);
  if (seconds < 60) return `${seconds} sec`;

  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder === 0 ? `${minutes} min` : `${minutes} min ${remainder} sec`;
}

/** 55.599999999999994 -> "55.6 m". Rounds away floating-point noise. */
const formatMetres = (metres) => `${metres.toFixed(1)} m`;

function setBadge(element, isTrue, trueText, falseText) {
  element.textContent = isTrue ? `✓ ${trueText}` : `✗ ${falseText}`;
  element.className = isTrue ? "badge yes" : "badge no";
}

// --------------------------------------------------------------------------
// Walking through the steps
// --------------------------------------------------------------------------

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
      // A photo looking the way this step goes, if one has been taken. The
      // backend only ever sends one facing the right direction.
      if (step.photo_url) {
        const photo = document.createElement("img");
        photo.className = "step-photo";
        photo.src = photoUrl(step.photo_url);
        photo.alt = `Looking towards ${nodeName(step.to_id)}`;
        photo.loading = "lazy";
        item.appendChild(photo);
      }

      const detail = document.createElement("p");
      detail.className = "step-detail";
      detail.textContent = step.detail;
      item.appendChild(detail);

      if (step.condition) {
        const warning = document.createElement("p");
        warning.className = "step-condition";
        warning.textContent = `Reported as ${step.condition}`;
        item.appendChild(warning);
      }

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
  nextButton.textContent =
    currentStepIndex === currentSteps.length - 1 ? "I'm here" : "Next step";
}

function showRoute(route) {
  clearOutput();

  // Time shown is time on the move plus any waiting, because that is what
  // "how long will this take me" means to somebody standing at a bus stop.
  const totalTime = route.total_walk_seconds + (route.total_wait_seconds ?? 0);
  totalTimeOutput.textContent = formatSeconds(totalTime);
  totalDistanceOutput.textContent = formatMetres(route.walking_distance_m ?? 0);

  // Only worth saying when the two numbers differ, which is when part of the
  // journey was a ride rather than a walk.
  const extras = [];
  if (route.total_wait_seconds > 0) {
    extras.push(`includes ${formatSeconds(route.total_wait_seconds)} waiting`);
  }
  if (route.total_distance_m > (route.walking_distance_m ?? 0)) {
    extras.push(`${formatMetres(route.total_distance_m)} travelled in total`);
  }
  totalExtra.textContent = extras.join(" · ");
  totalExtra.hidden = extras.length === 0;

  setBadge(stairsBadge, route.uses_stairs, "Uses stairs", "No stairs");
  setBadge(liftBadge, route.uses_lift, "Uses lift", "No lift");
  setBadge(shuttleBadge, route.uses_shuttle, "Uses shuttle", "No shuttle");
  setBadge(shelterBadge, route.fully_sheltered, "Sheltered", "Partly exposed");

  showRouteOnMap(route);

  currentSteps = route.steps ?? [];
  currentStepIndex = 0;

  if (currentSteps.length === 0) {
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


// --------------------------------------------------------------------------
// Other ways round
// --------------------------------------------------------------------------

function optionCard(option, isPrimary) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = isPrimary ? "route-option is-current" : "route-option";

  const label = document.createElement("span");
  label.className = "route-option-label";
  label.textContent = option.label;

  const why = document.createElement("span");
  why.className = "route-option-why";
  why.textContent = option.why;

  const numbers = document.createElement("span");
  numbers.className = "route-option-numbers";
  const time = option.route.total_walk_seconds + (option.route.total_wait_seconds ?? 0);
  numbers.textContent =
    `${formatSeconds(time)} · ${formatMetres(option.route.walking_distance_m ?? 0)} walking`;

  card.append(label, why, numbers);
  card.addEventListener("click", () => {
    for (const other of optionsList.querySelectorAll(".route-option")) {
      other.classList.remove("is-current");
    }
    card.classList.add("is-current");
    // showRoute only hides and refills the result card; it never touches this
    // list, so the options stay on screen to be switched between.
    showRoute(option.route);
  });
  return card;
}

function renderChoices(choices) {
  optionsList.replaceChildren();

  if (choices.alternatives.length === 0) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "No other way round is better in any way worth offering.";
    optionsList.appendChild(note);
  } else {
    [choices.primary, ...choices.alternatives].forEach((option, index) => {
      optionsList.appendChild(optionCard(option, index === 0));
    });
  }
  optionsList.hidden = false;
}

showOptionsButton.addEventListener("click", async () => {
  if (!lastRequest) return;

  if (!optionsList.hidden && optionsList.childElementCount > 0) {
    optionsList.hidden = true;
    showOptionsButton.textContent = "View other routes";
    return;
  }

  showOptionsButton.disabled = true;
  showOptionsButton.textContent = "Looking…";
  try {
    renderChoices(await requestRouteOptions(lastRequest));
    showOptionsButton.textContent = "Hide other routes";
  } catch (error) {
    showPlanError(error.message);
  } finally {
    showOptionsButton.disabled = false;
  }
});

/** The step the user is currently on, so a report can be about the right place. */
export function currentStep() {
  return currentSteps[currentStepIndex] ?? null;
}

// --------------------------------------------------------------------------
// Asking for a route
// --------------------------------------------------------------------------

function currentOptions() {
  const chosen = form.querySelector('input[name="preference"]:checked');
  return {
    preference: chosen ? chosen.value : "fastest",
    allow_stairs: allowStairsCheckbox.checked,
    allow_lift: allowLiftCheckbox.checked,
    allow_shuttle: allowShuttleCheckbox.checked,
    sheltered_only: shelteredOnlyCheckbox.checked,
  };
}

async function findRoute(origin, destination) {
  showPlanLoading("Finding the best route…");
  findButton.textContent = "Finding…";

  lastRequest = { origin, destination, ...currentOptions() };
  optionsList.replaceChildren();
  optionsList.hidden = true;
  showOptionsButton.textContent = "View other routes";

  try {
    const route = await requestRoute(lastRequest);
    stopPlanLoading();
    showRoute(route);
  } catch (error) {
    stopPlanLoading();
    showPlanError(error.message);
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault(); // stay on the page instead of reloading

  const origin = originBox.selectedId();
  const destination = destinationBox.selectedId();

  // A typed-but-unchosen box is the common mistake, and sending its raw text
  // would come back as a confusing "unknown node id" from the backend.
  if (!origin || !destination) {
    showPlanError(`Pick a ${!origin ? "start" : "destination"} from the suggestions.`);
    return;
  }

  findRoute(origin, destination);
});

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

// Start with an empty map rather than a blank panel.
showEmptyMap();

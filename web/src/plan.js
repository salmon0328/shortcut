// The route planner: pick two places, ask the backend, then walk the answer
// one step at a time.

import { photoUrl, requestRoute } from "./api.js";
import { nodeName } from "./data.js";
import { createSearchBox } from "./searchBox.js";

const form = document.querySelector("#route-form");
const findButton = document.querySelector("#find-button");
const loadingMessage = document.querySelector("#loading");
const errorMessage = document.querySelector("#error");

const allowStairsCheckbox = document.querySelector("#allow-stairs");
const allowLiftCheckbox = document.querySelector("#allow-lift");
const shelteredOnlyCheckbox = document.querySelector("#sheltered-only");

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

  totalTimeOutput.textContent = formatSeconds(route.total_walk_seconds);
  totalDistanceOutput.textContent = formatMetres(route.total_distance_m);

  setBadge(stairsBadge, route.uses_stairs, "Uses stairs", "No stairs");
  setBadge(liftBadge, route.uses_lift, "Uses lift", "No lift");
  setBadge(shelterBadge, route.fully_sheltered, "Sheltered", "Partly exposed");

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
    sheltered_only: shelteredOnlyCheckbox.checked,
  };
}

async function findRoute(origin, destination) {
  showPlanLoading("Finding the best route…");
  findButton.textContent = "Finding…";

  try {
    const route = await requestRoute({
      origin,
      destination,
      ...currentOptions(),
    });
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

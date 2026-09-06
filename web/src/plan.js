// The route planner: pick two places, ask the backend, then walk the answer
// one step at a time.
//
// Two screens share this file. The plan screen owns the form and the route
// summary; the steps screen owns the step-by-step list. They are one module
// because they show the same route, and the summary is the way in to the
// steps.

import {
  aiAvailable,
  parseSentence,
  photoUrl,
  requestRoute,
  requestRouteOptions,
} from "./api.js";
import { getNodes, nodeLabel, nodeName, placeWhere } from "./data.js";
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

// The summary, on the plan screen.
const resultCard = document.querySelector("#result");
const totalTimeOutput = document.querySelector("#total-time");
const summaryLine = document.querySelector("#summary-line");
const summaryFrom = document.querySelector("#summary-from");
const summaryTo = document.querySelector("#summary-to");
const stairsBadge = document.querySelector("#badge-stairs");
const liftBadge = document.querySelector("#badge-lift");
const shuttleBadge = document.querySelector("#badge-shuttle");
const shelterBadge = document.querySelector("#badge-shelter");
const totalExtra = document.querySelector("#total-extra");
const showOptionsButton = document.querySelector("#show-options");
const optionsList = document.querySelector("#route-options");

// The walk, on the steps screen.
const arrivalTime = document.querySelector("#arrival-time");
const stepsList = document.querySelector("#route-steps");
const stepProgress = document.querySelector("#step-progress");
const backButton = document.querySelector("#back-button");
const nextButton = document.querySelector("#next-button");
const finishButton = document.querySelector("#finish-button");

// The plain-language box.
const ask = document.querySelector("#ask");
const askInput = document.querySelector("#ask-input");
const askButton = document.querySelector("#ask-button");
const askStatus = document.querySelector("#ask-status");
const askChoices = document.querySelector("#ask-choices");

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
  findButton.textContent = "Get route";
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

/** Whole minutes, rounded up: nobody plans around "4 min 12 sec". */
function formatMinutes(totalSeconds) {
  const minutes = Math.max(1, Math.ceil(totalSeconds / 60));
  return `${minutes} min${minutes === 1 ? "" : "s"}`;
}

/** 55.599999999999994 -> "56 m". Whole metres are enough for a walk. */
const formatMetres = (metres) => `${Math.round(metres)} m`;

function setBadge(element, isTrue, trueText, falseText) {
  element.textContent = isTrue ? `✓ ${trueText}` : `✗ ${falseText}`;
  element.className = isTrue ? "badge yes" : "badge no";
}

/** "mostly sheltered", from how much of the walk is under cover. */
function shelterWording(route) {
  const steps = route.steps ?? [];
  const total = steps.reduce((sum, step) => sum + step.distance_m, 0);
  if (total === 0) return "";

  const covered = steps
    .filter((step) => step.covered)
    .reduce((sum, step) => sum + step.distance_m, 0);
  const share = covered / total;

  if (share >= 0.999) return "fully sheltered";
  if (share >= 0.6) return "mostly sheltered";
  if (share > 0) return "partly sheltered";
  return "uncovered";
}

// --------------------------------------------------------------------------
// Walking through the steps
// --------------------------------------------------------------------------

function photoOrPlaceholder(step) {
  // A photo looking the way this step goes, if one has been taken. The
  // backend only ever sends one facing the right direction.
  if (step.photo_url) {
    const photo = document.createElement("img");
    photo.className = "step-photo";
    photo.src = photoUrl(step.photo_url);
    photo.alt = `Looking towards ${nodeName(step.to_id)}`;
    photo.loading = "lazy";
    return photo;
  }

  const empty = document.createElement("div");
  empty.className = "step-photo-empty";
  empty.setAttribute("aria-hidden", "true");
  empty.innerHTML =
    '<svg viewBox="0 0 24 24" width="36" height="36">' +
    '<rect x="3" y="5" width="18" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<circle cx="8.5" cy="10" r="1.6" fill="currentColor"/>' +
    '<path d="M5 17l4.5-4.5 3 3 2.5-2.5L19 17" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>' +
    "</svg>";
  return empty;
}

function renderSteps() {
  stepsList.replaceChildren();

  currentSteps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = "step";
    if (index === currentStepIndex) item.classList.add("is-current");
    if (index < currentStepIndex) item.classList.add("is-done");

    // Any step can be jumped to. Walking a route is not a wizard: somebody
    // reading ahead to see what is coming, or back to check what they just
    // passed, should not have to click through every step in between. Back
    // and Next stay for walking it in order, which is still the common case.
    if (index !== currentStepIndex) {
      item.classList.add("is-jumpable");
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      item.setAttribute("aria-label", `Step ${index + 1}: ${step.instruction}`);

      const jump = () => {
        currentStepIndex = index;
        renderSteps();
      };
      item.addEventListener("click", jump);
      item.addEventListener("keydown", (event) => {
        // Enter and Space are what a button answers to, and this is one.
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          jump();
        }
      });
    }

    const heading = document.createElement("p");
    heading.className = "step-instruction";
    heading.textContent = step.instruction;
    item.appendChild(heading);

    // Only the step being walked shows its full description, so the list
    // stays scannable.
    if (index === currentStepIndex) {
      item.appendChild(photoOrPlaceholder(step));

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

      // The Back button belongs to the card being walked, as in the sketch.
      // Moving the element keeps its click handler.
      backButton.className = "secondary small step-back";
      backButton.hidden = false;
      item.appendChild(backButton);
    }

    stepsList.appendChild(item);
  });

  const arrived = currentStepIndex >= currentSteps.length;

  // Shown whether or not it has been reached, so the end of the journey can
  // be jumped to like any other step - and so the list does not change length
  // underneath somebody clicking down it.
  const done = document.createElement("li");
  done.className = "step step-arrived";
  done.textContent = "You have arrived.";

  if (arrived) {
    done.classList.add("is-current");
    backButton.className = "secondary small step-back";
    backButton.hidden = false;
    done.appendChild(backButton);
  } else {
    done.classList.add("is-jumpable");
    done.tabIndex = 0;
    done.setAttribute("role", "button");
    done.setAttribute("aria-label", "Jump to the end of the journey");

    const jump = () => {
      currentStepIndex = currentSteps.length;
      renderSteps();
    };
    done.addEventListener("click", jump);
    done.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        jump();
      }
    });
  }
  stepsList.appendChild(done);

  // How long is left, counted from the step being walked. Waits count: a
  // lift you have to stand around for is part of getting there.
  const remaining = currentSteps
    .slice(currentStepIndex)
    .reduce((sum, step) => sum + step.walk_seconds + (step.wait_seconds ?? 0), 0);
  arrivalTime.textContent = arrived
    ? "You have arrived"
    : `${formatMinutes(remaining)} to arrival`;

  stepProgress.textContent = arrived
    ? "Journey complete"
    : `Step ${currentStepIndex + 1} of ${currentSteps.length}`;

  backButton.disabled = currentStepIndex === 0;
  nextButton.textContent =
    currentStepIndex === currentSteps.length - 1 ? "I'm here" : "I'm here, next step";

  // Once there, "next step" has nothing left to do, so its place goes to
  // the way out.
  nextButton.hidden = arrived;
  finishButton.hidden = !arrived;
}

/** Forget the route and the places, back to the blank plan screen. */
export function resetPlan() {
  originBox.clear();
  destinationBox.clear();
  currentSteps = [];
  currentStepIndex = 0;
  lastRequest = null;
  optionsList.replaceChildren();
  optionsList.hidden = true;
  showOptionsButton.textContent = "View others";
  stepsList.replaceChildren();
  nextButton.hidden = false;
  finishButton.hidden = true;
  askInput.value = "";
  clearAsk();
  clearOutput();
  showEmptyMap();
}

function showRoute(route) {
  clearOutput();

  // Time shown is time on the move plus any waiting, because that is what
  // "how long will this take me" means to somebody standing at a bus stop.
  const totalTime = route.total_walk_seconds + (route.total_wait_seconds ?? 0);
  totalTimeOutput.textContent = formatMinutes(totalTime);

  const shelter = shelterWording(route);
  summaryLine.textContent = shelter
    ? `${formatMetres(route.walking_distance_m ?? 0)}, ${shelter}`
    : formatMetres(route.walking_distance_m ?? 0);

  summaryFrom.textContent = nodeLabel(route.nodes[0]);
  summaryTo.textContent = nodeLabel(route.nodes[route.nodes.length - 1]);

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
    arrivalTime.textContent = "You are already there";
    stepProgress.textContent = "";
    backButton.hidden = true;
    nextButton.hidden = true;
    finishButton.hidden = false;
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
    // showRoute only hides and refills the summary; it never touches this
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
    showOptionsButton.textContent = "View others";
    return;
  }

  showOptionsButton.disabled = true;
  showOptionsButton.textContent = "Looking…";
  try {
    renderChoices(await requestRouteOptions(lastRequest));
    showOptionsButton.textContent = "Hide others";
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
  showOptionsButton.textContent = "View others";

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

// --------------------------------------------------------------------------
// Saying it in a sentence
// --------------------------------------------------------------------------
//
// This box is a front end to the two pickers, not a second way to route. A
// sentence is read into the same RouteRequest the pickers build, the controls
// are set to show what was understood, and the ordinary route call runs. So
// nothing the sentence decided is hidden, and all of it can be corrected by
// hand afterwards.

function setAskStatus(text, kind = "") {
  askStatus.textContent = text;
  askStatus.className = kind ? `ask-status ${kind}` : "ask-status";
  askStatus.hidden = !text;
}

function clearAsk() {
  setAskStatus("");
  askChoices.replaceChildren();
  askChoices.hidden = true;
}

/** Put a resolved place into one of the pickers, by its node id. */
function fillBox(box, nodeId) {
  const node = getNodes().find((candidate) => candidate.id === nodeId);
  if (node) box.select(node);
  return Boolean(node);
}

/**
 * Show what was understood on the controls themselves.
 *
 * Applied on every answer, not only the ones that produced a route. "Take me
 * to the lift lobby, I can't use stairs" is a question about the lift lobby
 * *and* a refusal of stairs, and the refusal has to be on screen before the
 * question is answered - otherwise answering it walks the student up a
 * staircase they have just ruled out.
 */
function applyToControls(preferences) {
  const chip = form.querySelector(
    `input[name="preference"][value="${preferences.preference}"]`
  );
  if (chip) chip.checked = true;
  allowStairsCheckbox.checked = preferences.allow_stairs;
  allowLiftCheckbox.checked = preferences.allow_lift;
  allowShuttleCheckbox.checked = preferences.allow_shuttle;
  shelteredOnlyCheckbox.checked = preferences.sheltered_only;
}

/**
 * Which end the backend's question is about.
 *
 * The same precedence as `_first_question` in shortcut/ai/parser.py:
 * destination first, because that is the half people leave vague. Guessing
 * differently here would offer the choices for one end under a question about
 * the other - and both ends can be ambiguous at once, so that is a real risk
 * rather than a theoretical one.
 */
function endInQuestion(result) {
  const askedAboutDestination =
    result.destination.ambiguous || result.destination.resolved === null;
  return askedAboutDestination ? "destination" : "origin";
}

const boxFor = (endName) => (endName === "origin" ? originBox : destinationBox);

/**
 * Ask about one end, and keep asking until both are settled.
 *
 * A sentence can be vague at both ends - "take me to the lift lobby from
 * staircase 1" is ambiguous twice over. The backend deliberately asks one
 * question at a time, so answering the first has to raise the second here
 * rather than leaving an empty box and no prompt.
 *
 * Re-parsing instead would be the obvious move and the wrong one: the
 * sentence has not changed, so it would come back just as ambiguous.
 */
function askAbout(result, endName, question) {
  const choice = result[endName];
  const box = boxFor(endName);

  setAskStatus(question, "asking");
  askChoices.replaceChildren();

  for (const candidate of choice.alternatives) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ask-choice";
    button.textContent = `${candidate.name} (${placeWhere(candidate)})`;
    button.addEventListener("click", () => {
      fillBox(box, candidate.node_id);
      advance(result);
    });
    askChoices.appendChild(button);
  }

  askChoices.hidden = choice.alternatives.length === 0;
  // Nothing to choose from means the place is not on the map at all, so the
  // picker beneath is the only way forward.
  if (choice.alternatives.length === 0) box.focus();
}

/** Route if both ends are settled, otherwise ask about the one that is not. */
function advance(result) {
  const origin = originBox.selectedId();
  const destination = destinationBox.selectedId();

  if (origin && destination) {
    clearAsk();
    // Everything the sentence said about preferences is already on the
    // controls, so running what the screen shows is running what was asked.
    findRoute(origin, destination);
    return;
  }

  const endName = origin ? "destination" : "origin";
  const choice = result[endName];
  const where = endName === "origin" ? "starting point" : "destination";

  askAbout(
    result,
    endName,
    choice.alternatives.length > 0
      ? `And which ${choice.phrase || where} did you mean?`
      : `Now pick your ${where} below.`
  );
}

async function runAsk() {
  const text = askInput.value.trim();
  if (!text) return;

  clearAsk();
  clearOutput();
  askButton.disabled = true;
  setAskStatus("Reading that…");

  // Sent only when both places are already chosen, because a RouteRequest
  // cannot be built without them. It carries the preferences, so a sentence
  // that says nothing about stairs leaves the toggle alone - and when it is
  // not sent, the controls below still show whatever came back.
  const origin = originBox.selectedId();
  const destination = destinationBox.selectedId();
  const current =
    origin && destination ? { origin, destination, ...currentOptions() } : null;

  try {
    const result = await parseSentence(text, current);

    // Whatever it did work out goes onto the screen, even when the other end
    // is still a question: half an answer is still progress worth keeping,
    // and the preferences have to be showing before any question is answered.
    applyToControls(result.preferences);
    if (result.origin.resolved) fillBox(originBox, result.origin.resolved.node_id);
    if (result.destination.resolved) {
      fillBox(destinationBox, result.destination.resolved.node_id);
    }

    if (result.request) {
      clearAsk();
      findRoute(result.request.origin, result.request.destination);
      return;
    }

    askAbout(
      result,
      endInQuestion(result),
      result.question ?? "Which place did you mean?"
    );
  } catch (error) {
    setAskStatus(error.message, "error");
  } finally {
    askButton.disabled = false;
  }
}

askButton.addEventListener("click", runAsk);

askInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault(); // the box is outside the form; do not submit it
    runAsk();
  }
});

// Hidden unless the backend actually has the AI routes mounted.
aiAvailable().then((available) => {
  ask.hidden = !available;
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

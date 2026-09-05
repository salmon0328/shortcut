// Shortcut frontend: loads the map data, then wires the screens together.
// The screens themselves live in plan.js, report.js and admin.js.
//
// No AI, no map drawing, no libraries. Everything shown here comes from the
// backend, which in turn reads data/campus_graph.json.

import "./style.css";

import { refreshQueue } from "./admin.js";
import { refreshMapEditor } from "./adminMap.js";
import { loadReferenceData } from "./data.js";
import { refreshPending } from "./pending.js";
import {
  currentStep,
  resetPlan,
  showPlanError,
  showPlanLoading,
  stopPlanLoading,
} from "./plan.js";
import { prefillFromStep, resetForm } from "./report.js";

// One screen per section, in the order the wireframe walks them: splash,
// plan, steps, report; admin sits off to the side behind its switch.
const views = {
  splash: document.querySelector("#splash-view"),
  plan: document.querySelector("#plan-view"),
  steps: document.querySelector("#steps-view"),
  report: document.querySelector("#report-view"),
  admin: document.querySelector("#admin-view"),
};

const startButton = document.querySelector("#start-button");
const startWalkingButton = document.querySelector("#start-walking");
const finishButton = document.querySelector("#finish-button");
const adminToggle = document.querySelector("#admin-toggle");
const reportProblemButtons = document.querySelectorAll("[data-report-problem]");

/** Show one screen and hide the rest. */
function showView(name) {
  for (const [key, element] of Object.entries(views)) {
    element.hidden = key !== name;
  }
  window.scrollTo(0, 0);
}

// --------------------------------------------------------------------------
// Moving between screens
// --------------------------------------------------------------------------

startButton.addEventListener("click", () => showView("plan"));

startWalkingButton.addEventListener("click", () => showView("steps"));

// Arrived: wipe the route and the place boxes, so the next journey starts
// from a clean screen rather than the last one's answer.
finishButton.addEventListener("click", () => {
  resetPlan();
  showView("plan");
});

for (const button of document.querySelectorAll("[data-back-to-plan]")) {
  button.addEventListener("click", () => {
    adminToggle.checked = false;
    showView("plan");
  });
}

// One on the plan screen, one on the steps screen. Both open the same form;
// only whether it comes pre-filled differs.
for (const button of reportProblemButtons) {
  button.addEventListener("click", () => {
    const step = button.closest("#steps-view") ? currentStep() : null;
    // Reporting from a step already knows which corridor is meant, so the
    // form opens pointed at it rather than making the user find it again.
    // From the plan screen nothing is being walked, so it opens blank.
    if (step) {
      prefillFromStep(step);
    } else {
      resetForm();
    }
    showView("report");
  });
}

// Admin mode is only a switch in this browser. It does not protect anything:
// the review endpoints are open, which is fine while this runs locally and is
// the first thing to change before anyone else can reach it.
adminToggle.addEventListener("change", () => {
  if (adminToggle.checked) {
    showView("admin");
    showAdminTab("reports");
  } else {
    showView("plan");
  }
});

// --------------------------------------------------------------------------
// The panels of admin mode
// --------------------------------------------------------------------------

const adminTabs = {
  reports: document.querySelector("#admin-tab-reports"),
  "add-place": document.querySelector("#admin-tab-add-place"),
  "add-link": document.querySelector("#admin-tab-add-link"),
  edit: document.querySelector("#admin-tab-edit"),
  pending: document.querySelector("#admin-tab-pending"),
};

function showAdminTab(name) {
  for (const [key, element] of Object.entries(adminTabs)) {
    element.hidden = key !== name;
  }
  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("is-active", button.dataset.tab === name);
  }

  // Every panel reads the map, which another one may have just changed.
  if (name === "reports") {
    refreshQueue();
  } else if (name === "pending") {
    refreshPending();
  } else {
    refreshMapEditor();
  }
}

for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", () => showAdminTab(button.dataset.tab));
}

// --------------------------------------------------------------------------
// Start
// --------------------------------------------------------------------------

async function start() {
  // Loads behind the splash screen, so by the time someone taps through the
  // place lists are ready.
  showPlanLoading("Loading locations…");
  try {
    await loadReferenceData();
    stopPlanLoading();
  } catch (error) {
    // Leave the Get route button disabled: with no places there is nothing
    // to route between, so re-enabling it would only produce a second error.
    showPlanError(error.message);
  }
}

showView("splash");
start();

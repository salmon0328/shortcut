// Shortcut frontend: loads the map data, then wires the three screens
// together. The screens themselves live in plan.js, report.js and admin.js.
//
// No AI, no map drawing, no libraries. Everything shown here comes from the
// backend, which in turn reads data/campus_graph.json.

import "./style.css";

import { refreshQueue } from "./admin.js";
import { loadReferenceData } from "./data.js";
import { currentStep, showPlanError, showPlanLoading, stopPlanLoading } from "./plan.js";
import { prefillFromStep, resetForm } from "./report.js";

const views = {
  plan: document.querySelector("#plan-view"),
  report: document.querySelector("#report-view"),
  admin: document.querySelector("#admin-view"),
};

const adminToggle = document.querySelector("#admin-toggle");
const reportProblemButton = document.querySelector("#report-problem-button");

/** Show one screen and hide the rest. */
function showView(name) {
  for (const [key, element] of Object.entries(views)) {
    element.hidden = key !== name;
  }
}

// --------------------------------------------------------------------------
// Moving between screens
// --------------------------------------------------------------------------

for (const button of document.querySelectorAll("[data-back-to-plan]")) {
  button.addEventListener("click", () => {
    adminToggle.checked = false;
    showView("plan");
  });
}

reportProblemButton.addEventListener("click", () => {
  const step = currentStep();
  // Reporting from a step already knows which corridor is meant, so the form
  // opens pointed at it rather than making the user find it again.
  if (step) {
    prefillFromStep(step);
  } else {
    resetForm();
  }
  showView("report");
});

// Admin mode is only a switch in this browser. It does not protect anything:
// the review endpoints are open, which is fine while this runs locally and is
// the first thing to change before anyone else can reach it.
adminToggle.addEventListener("change", () => {
  if (adminToggle.checked) {
    showView("admin");
    refreshQueue();
  } else {
    showView("plan");
  }
});

// --------------------------------------------------------------------------
// Start
// --------------------------------------------------------------------------

async function start() {
  showPlanLoading("Loading locations…");
  try {
    await loadReferenceData();
    stopPlanLoading();
  } catch (error) {
    // Leave the Find button disabled: with no places there is nothing to
    // route between, so re-enabling it would only produce a second error.
    showPlanError(error.message);
  }
}

showView("plan");
start();

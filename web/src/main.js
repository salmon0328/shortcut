// Shortcut frontend: loads the map data, then wires the three screens
// together. The screens themselves live in plan.js, report.js and admin.js.
//
// No AI, no map drawing, no libraries. Everything shown here comes from the
// backend, which in turn reads data/campus_graph.json.

import "./style.css";

import { refreshQueue } from "./admin.js";
import { refreshMapEditor } from "./adminMap.js";
import { loadReferenceData } from "./data.js";
import { refreshPending } from "./pending.js";
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
    showAdminTab("reports");
  } else {
    showView("plan");
  }
});

// --------------------------------------------------------------------------
// The two halves of admin mode
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

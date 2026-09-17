// Shortcut frontend: loads the map data, then wires the screens together.
// The screens themselves live in plan.js, report.js and admin.js.
//
// No AI, no map drawing, no libraries. Everything shown here comes from the
// backend, which in turn reads data/campus_graph.json.

import "./style.css";

import { refreshQueue } from "./admin.js";
import { refreshMapEditor } from "./adminMap.js";
import { refresh as refreshImport } from "./importReview.js";
import { showFloor } from "./floorMap.js";
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
import { initThemeToggle } from "./theme.js";
import { initCampus3d } from "./campus3d.js";

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
const endButton = document.querySelector("#end-button");
const adminButton = document.querySelector("#admin-button");
const tabbar = document.querySelector("#tabbar");
const reportProblemButtons = document.querySelectorAll("[data-report-problem]");

// Which screen is showing, so the Report tab knows whether a walk is under way.
let currentView = "splash";

// Remembered so the splash greets a first visit only. Storage can be missing
// or throw; then the splash simply shows every time, as it always did.
const SPLASH_KEY = "shortcut-seen-splash";

function splashSeen() {
  try {
    return localStorage.getItem(SPLASH_KEY) === "1";
  } catch {
    return false;
  }
}

function markSplashSeen() {
  try {
    localStorage.setItem(SPLASH_KEY, "1");
  } catch {
    // Shown again next time, which is harmless.
  }
}

/** Show one screen and hide the rest. */
function showView(name) {
  currentView = name;
  for (const [key, element] of Object.entries(views)) {
    element.hidden = key !== name;
  }

  // The tab bar is for the student's three screens only.
  tabbar.hidden = name === "splash" || name === "admin";
  for (const tab of tabbar.querySelectorAll("[data-nav]")) {
    if (tab.dataset.nav === name) {
      tab.setAttribute("aria-current", "page");
    } else {
      tab.removeAttribute("aria-current");
    }
  }

  window.scrollTo(0, 0);
  // Maps drawn while hidden have to be told they can be seen now.
  document.dispatchEvent(new CustomEvent("shortcut:view", { detail: name }));
}

// --------------------------------------------------------------------------
// Moving between screens
// --------------------------------------------------------------------------

startButton.addEventListener("click", () => {
  markSplashSeen();
  showView("plan");
});

startWalkingButton.addEventListener("click", () => showView("steps"));

// Arrived: wipe the route and the place boxes, so the next journey starts
// from a clean screen rather than the last one's answer.
finishButton.addEventListener("click", () => {
  resetPlan();
  showView("plan");
});

// Ending part-way through throws the walk away, so it asks first. The Plan
// tab is the way to look back at the route without ending anything.
endButton.addEventListener("click", () => {
  if (finishButton.hidden && !window.confirm("End navigation?")) return;
  resetPlan();
  showView("plan");
});

for (const button of document.querySelectorAll("[data-back-to-plan]")) {
  button.addEventListener("click", () => showView("plan"));
}

// Called by other modules (the report's "Done" buttons) to move on.
document.addEventListener("shortcut:navigate", (event) => {
  showView(event.detail);
});

// Plan and Walk tabs. Report is handled with the other report buttons below.
for (const tab of tabbar.querySelectorAll("[data-nav]:not([data-report-problem])")) {
  tab.addEventListener("click", () => {
    if (!tab.disabled) showView(tab.dataset.nav);
  });
}

// One on the steps screen, and the Report tab. Both open the same form; only
// whether it comes pre-filled differs.
for (const button of reportProblemButtons) {
  button.addEventListener("click", () => {
    // Already on the form: the tab does nothing, so a half-written report is
    // not wiped by a stray tap.
    if (currentView === "report") return;
    const step = currentView === "steps" ? currentStep() : null;
    // Reporting from a step already knows which corridor is meant, so the
    // form opens pointed at it rather than making the user find it again.
    // From anywhere else nothing is being walked, so it opens blank.
    if (step) {
      prefillFromStep(step);
    } else {
      resetForm();
    }
    showView("report");
  });
}

// Admin mode is only a button in this browser. It does not protect anything:
// the review endpoints are open, which is fine while this runs locally and is
// the first thing to change before anyone else can reach it.
adminButton.addEventListener("click", () => {
  showView("admin");
  showAdminTab("reports");
});

initThemeToggle(document.querySelector("#theme-button"));

// --------------------------------------------------------------------------
// The panels of admin mode
// --------------------------------------------------------------------------

const adminTabs = {
  reports: document.querySelector("#admin-tab-reports"),
  edit: document.querySelector("#admin-tab-edit"),
  import: document.querySelector("#admin-tab-import"),
  map: document.querySelector("#admin-tab-map"),
  pending: document.querySelector("#admin-tab-pending"),
};

// Adding and editing were three tabs of the same job, which made the row long
// enough to push the rest off the edge. They are one tab now, with a sub-nav:
// the panels are still three separate elements, only their visibility is
// shared.
const editPanels = {
  edit: adminTabs.edit,
  "add-place": document.querySelector("#admin-tab-add-place"),
  "add-link": document.querySelector("#admin-tab-add-link"),
};
const editSubtabs = document.querySelector("#edit-subtabs");
let editMode = "edit";

function showAdminTab(name) {
  const editing = name === "edit";

  for (const [key, element] of Object.entries(adminTabs)) {
    if (key !== "edit") element.hidden = key !== name;
  }
  editSubtabs.hidden = !editing;
  for (const [key, element] of Object.entries(editPanels)) {
    element.hidden = !editing || key !== editMode;
  }

  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("is-active", button.dataset.tab === name);
  }

  // Every panel reads the map, which another one may have just changed.
  if (name === "reports") {
    refreshQueue();
  } else if (name === "pending") {
    refreshPending();
  } else if (name === "import") {
    refreshImport();
  } else if (name === "map") {
    refreshFloorMap();
  } else {
    refreshMapEditor();
  }
}

for (const button of editSubtabs.querySelectorAll(".subtab")) {
  button.addEventListener("click", () => {
    editMode = button.dataset.edit;
    for (const other of editSubtabs.querySelectorAll(".subtab")) {
      other.classList.toggle("is-active", other === button);
    }
    showAdminTab("edit");
  });
}

const floorMapCanvas = document.querySelector("#floor-map-canvas");
const floorMapTabs = document.querySelector("#floor-map-tabs");

/** Redraw the floor map, keeping whichever floor is already being looked at. */
function refreshFloorMap() {
  const active = floorMapTabs.querySelector(".floor-tab.is-active");
  showFloor(floorMapCanvas, floorMapTabs, active?.dataset.key ?? null);
}

document
  .querySelector("#refresh-floor-map")
  .addEventListener("click", refreshFloorMap);

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
    // After the places, which the 3D map needs to know what floor each is on.
    initCampus3d();
  } catch (error) {
    // Leave the Get route button disabled: with no places there is nothing
    // to route between, so re-enabling it would only produce a second error.
    showPlanError(error.message);
  }
}

showView(splashSeen() ? "plan" : "splash");
start();

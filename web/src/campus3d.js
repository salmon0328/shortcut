// Which map a route is drawn on: the 3D campus, or the flat floorplan.
//
// This module is small on purpose and loads with the page. The 3D map itself
// (map3dView.js, and the two libraries behind it) is fetched the first time
// it is needed, and only on a device that can draw it, for a campus that has
// been placed on the globe. Everywhere else the floorplan view in mapView.js
// carries on exactly as before, so every failure here ends in that view
// rather than in a blank panel.

import { fetchGeo } from "./api.js";

const MODE_KEY = "shortcut-map-mode";
const WALK_MAP_KEY = "shortcut-walk-map";

const panel = document.querySelector("#map-panel");
const planHolder = document.querySelector("#map3d");
const planCanvas = document.querySelector("#map3d-canvas");
const modeSwitch = document.querySelector("#map-mode");

const walkHolder = document.querySelector("#walk-map");
const walkCanvas = document.querySelector("#walk-map-canvas");
const walkToggle = document.querySelector("#walk-map-toggle");

let geo = null;
let available = false;
let viewModule = null;
let planMap = null;
let walkMap = null;
let route = null;
let routePlaced = false;
let stepIndex = 0;

function readSetting(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function saveSetting(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Remembered for this visit only.
  }
}

let mode = readSetting(MODE_KEY, "3d");
let walkMapOpen = readSetting(WALK_MAP_KEY, "open") === "open";

function webglWorks() {
  try {
    return Boolean(document.createElement("canvas").getContext("webgl2"));
  } catch {
    return false;
  }
}

/** Load the heavy module once; null if it cannot be loaded. */
async function loadView() {
  if (viewModule) return viewModule;
  try {
    viewModule = await import("./map3dView.js");
  } catch (error) {
    console.warn("3D map unavailable:", error);
    disable();
  }
  return viewModule;
}

/** Give up on 3D for this visit and show the floorplans. */
function disable() {
  available = false;
  modeSwitch.hidden = true;
  syncPlanPanel();
  syncWalkStrip();
}

const showing3d = () => available && mode === "3d" && (route === null || routePlaced);

// --------------------------------------------------------------------------
// The plan screen's panel
// --------------------------------------------------------------------------

function syncPlanPanel() {
  const on = showing3d();
  panel.classList.toggle("is-3d", on);
  planHolder.hidden = !on;
  for (const button of modeSwitch.querySelectorAll("[data-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === (on ? "3d" : "plan")));
  }
  if (on) ensurePlanMap();
}

async function ensurePlanMap() {
  if (planMap) return planMap;
  planHolder.classList.add("is-loading");
  const view = await loadView();
  if (!view) return null;
  if (!planMap) {
    planMap = new view.CampusMap(planCanvas, geo, {
      floorsEl: document.querySelector("#map3d-floors"),
      noteEl: document.querySelector("#map3d-note"),
    });
    planMap.loaded
      .then(() => planHolder.classList.remove("is-loading"))
      .catch((error) => {
        console.warn("3D base map failed to load:", error);
        disable();
      });
    if (route) planMap.showRoute(route);
  }
  return planMap;
}

for (const button of modeSwitch.querySelectorAll("[data-mode]")) {
  button.addEventListener("click", () => {
    mode = button.dataset.mode;
    saveSetting(MODE_KEY, mode);
    syncPlanPanel();
    syncWalkStrip();
  });
}

// --------------------------------------------------------------------------
// The walk screen's strip
// --------------------------------------------------------------------------

function syncWalkStrip() {
  const on = showing3d() && route !== null;
  walkHolder.hidden = !on;
  walkHolder.classList.toggle("is-collapsed", !walkMapOpen);
  walkToggle.textContent = walkMapOpen ? "Hide map" : "Show map";
  walkToggle.setAttribute("aria-expanded", String(walkMapOpen));
  if (on && walkMapOpen) ensureWalkMap();
}

async function ensureWalkMap() {
  if (walkMap) return walkMap;
  const view = await loadView();
  if (!view) return null;
  if (!walkMap) {
    walkMap = new view.CampusMap(walkCanvas, geo, {
      compact: true,
      floorsEl: document.querySelector("#walk-map-floor"),
      noteEl: document.querySelector("#walk-map-note"),
    });
    walkMap.loaded.catch(() => disable());
    if (route) {
      walkMap.showRoute(route);
      walkMap.focusStep(stepIndex);
    }
  }
  return walkMap;
}

walkToggle.addEventListener("click", () => {
  walkMapOpen = !walkMapOpen;
  saveSetting(WALK_MAP_KEY, walkMapOpen ? "open" : "closed");
  syncWalkStrip();
  if (walkMapOpen) walkMap?.resume();
});

// --------------------------------------------------------------------------
// What plan.js calls
// --------------------------------------------------------------------------

/** Does this route touch anything that is on the globe? */
function isPlaced(candidate) {
  const places = geo?.places ?? {};
  return (candidate.nodes ?? []).filter((id) => id in places).length >= 2;
}

export function showRoute(newRoute) {
  route = newRoute;
  stepIndex = 0;
  routePlaced = available && isPlaced(newRoute);
  syncPlanPanel();
  syncWalkStrip();
  if (!showing3d()) return;
  planMap?.showRoute(newRoute);
  if (walkMap) {
    walkMap.showRoute(newRoute);
    walkMap.focusStep(0);
  }
}

export function clearRoute() {
  route = null;
  routePlaced = false;
  stepIndex = 0;
  syncPlanPanel();
  syncWalkStrip();
  planMap?.clearRoute();
  walkMap?.clearRoute();
}

export function showStep(index) {
  stepIndex = index;
  if (showing3d()) walkMap?.focusStep(index);
}

// Maps drawn while their screen was hidden need a nudge when it shows.
document.addEventListener("shortcut:view", (event) => {
  if (event.detail === "plan") planMap?.resume();
  if (event.detail === "steps") walkMap?.resume();
});

/** Decide, once, whether this device and this campus get a 3D map. */
export async function initCampus3d() {
  if (!webglWorks()) return;
  try {
    geo = await fetchGeo();
  } catch {
    return; // no geo, no 3D: the floorplans carry on
  }
  if (!geo?.buildings?.length) return;

  available = true;
  modeSwitch.hidden = false;
  if (route) routePlaced = isPlaced(route);
  syncPlanPanel();
  syncWalkStrip();
}

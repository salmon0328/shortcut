// The map panel: a floorplan with the route drawn on it.
//
// None of the data this needs has been collected yet - there are no floorplan
// images, and no place has been given coordinates - so most of the time this
// shows a placeholder saying exactly which piece is missing. That is the point
// of building it now: the moment a plan is uploaded and calibrated, and places
// are surveyed, routes start being drawn with no further work.
//
// Everything is drawn inside one <svg> whose coordinate system is the
// floorplan image's own pixels. Putting the image *in* the SVG rather than
// behind it means the route lines line up with the plan at any size, with no
// arithmetic to keep the two in step.

import { fetchFloorplan, photoUrl } from "./api.js";
import { getNodes, nodeName } from "./data.js";

const SVG_NS = "http://www.w3.org/2000/svg";

const panel = document.querySelector("#map-panel");
const floorTabs = document.querySelector("#map-floors");
const canvas = document.querySelector("#map-canvas");

// The route being shown, and which floor of it the user is looking at.
let currentRoute = null;
let currentFloorKey = null;

function element(name, attributes = {}) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  return node;
}

function showMessage(title, detail) {
  canvas.replaceChildren();
  const box = document.createElement("div");
  box.className = "map-placeholder";

  const heading = document.createElement("p");
  heading.className = "map-placeholder-title";
  heading.textContent = title;
  box.appendChild(heading);

  if (detail) {
    const note = document.createElement("p");
    note.className = "map-placeholder-detail";
    note.textContent = detail;
    box.appendChild(note);
  }

  canvas.appendChild(box);
}

/** Load an image just to find out how big it is, in its own pixels. */
function measureImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.addEventListener("load", () =>
      resolve({ width: image.naturalWidth, height: image.naturalHeight })
    );
    image.addEventListener("error", () => reject(new Error("image failed to load")));
    image.src = url;
  });
}

/** Which building and floor each part of the route is on. */
function floorsOfRoute(route) {
  const byId = new Map(getNodes().map((node) => [node.id, node]));
  const floors = [];
  const seen = new Set();

  for (const nodeId of route.nodes) {
    const node = byId.get(nodeId);
    if (!node) continue;
    const key = `${node.building}|${node.floor}`;
    if (!seen.has(key)) {
      seen.add(key);
      floors.push({ key, building: node.building, floor: node.floor });
    }
  }
  return floors;
}

/** Where a place sits on this plan, in image pixels, or null if unknown. */
function toPixels(plan, node) {
  if (node.x === null || node.y === null) return null;
  return {
    x: (node.x - plan.origin_x_m) / plan.metres_per_pixel,
    y: (node.y - plan.origin_y_m) / plan.metres_per_pixel,
  };
}

function drawRouteOn(svg, plan, route, building, floor) {
  const byId = new Map(getNodes().map((node) => [node.id, node]));

  // Only the part of the journey on this floor. A route through three floors
  // is three separate lines, not one line jumping between plans.
  const points = [];
  for (const nodeId of route.nodes) {
    const node = byId.get(nodeId);
    if (!node || node.building !== building || node.floor !== floor) {
      continue;
    }
    const at = toPixels(plan, node);
    if (at) points.push({ ...at, node });
  }

  if (points.length === 0) {
    return 0;
  }

  if (points.length > 1) {
    svg.appendChild(
      element("polyline", {
        class: "map-route-line",
        points: points.map((point) => `${point.x},${point.y}`).join(" "),
      })
    );
  }

  points.forEach((point, index) => {
    const isStart = point.node.id === route.nodes[0];
    const isEnd = point.node.id === route.nodes[route.nodes.length - 1];
    const marker = element("circle", {
      cx: point.x,
      cy: point.y,
      r: isStart || isEnd ? 9 : 5,
      class: isStart ? "map-pin start" : isEnd ? "map-pin end" : "map-pin",
    });
    marker.appendChild(element("title")).textContent = nodeName(point.node.id);
    svg.appendChild(marker);
    void index;
  });

  return points.length;
}

async function renderFloor(route, floorInfo) {
  const { building, floor } = floorInfo;

  let plan;
  try {
    plan = await fetchFloorplan(building, floor);
  } catch (error) {
    showMessage("Could not load the floorplan", error.message);
    return;
  }

  if (!plan) {
    showMessage(
      `No floorplan for ${building} · Level ${floor} yet`,
      "Upload one in admin mode and the route will be drawn on it."
    );
    return;
  }

  if (!plan.is_calibrated) {
    showMessage(
      `${building} · Level ${floor} has not been measured`,
      "The plan is uploaded, but nothing says where the map sits on it yet. " +
        "Set a scale and origin in admin mode."
    );
    return;
  }

  let size;
  try {
    size = await measureImage(photoUrl(plan.url));
  } catch {
    showMessage("The floorplan image could not be loaded", plan.url);
    return;
  }

  const svg = element("svg", {
    class: "map-svg",
    viewBox: `0 0 ${size.width} ${size.height}`,
    preserveAspectRatio: "xMidYMid meet",
  });
  svg.appendChild(
    element("image", {
      href: photoUrl(plan.url),
      x: 0,
      y: 0,
      width: size.width,
      height: size.height,
    })
  );

  const drawn = route ? drawRouteOn(svg, plan, route, building, floor) : 0;

  canvas.replaceChildren(svg);

  if (route && drawn === 0) {
    const note = document.createElement("p");
    note.className = "map-note";
    note.textContent =
      "The plan is ready, but the places on this floor have not been " +
      "given positions yet, so the route cannot be drawn.";
    canvas.appendChild(note);
  }
}

function showFloorTabs(floors) {
  floorTabs.replaceChildren();
  if (floors.length < 2) {
    floorTabs.hidden = true;
    return;
  }

  for (const info of floors) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "floor-tab";
    button.classList.toggle("is-active", info.key === currentFloorKey);
    button.textContent = `${info.building} · ${info.floor}`;
    button.addEventListener("click", () => {
      currentFloorKey = info.key;
      showFloorTabs(floors);
      renderFloor(currentRoute, info);
    });
    floorTabs.appendChild(button);
  }
  floorTabs.hidden = false;
}

/** Draw a route, across however many floors it covers. */
export function showRouteOnMap(route) {
  currentRoute = route;
  const floors = floorsOfRoute(route);

  if (floors.length === 0) {
    showMessage("Nothing to show yet", null);
    floorTabs.hidden = true;
    return;
  }

  currentFloorKey = floors[0].key;
  showFloorTabs(floors);
  renderFloor(route, floors[0]);
}

/** The empty state, before anyone has asked for a route. */
export function showEmptyMap() {
  currentRoute = null;
  floorTabs.hidden = true;
  showMessage("Your route will appear here", null);
}

export { panel as mapPanel };

// Every place and link on one floor, drawn on its plan.
//
// The route map answers "how do I get there". This answers a different
// question, and an editor's one: *what does the map actually think this floor
// looks like* - which places are on it, what joins them, and what is missing.
// Reading that out of JSON is possible and nobody does it; seeing a staircase
// with nothing attached takes a second.
//
// Cross-floor links are the awkward part. A staircase joins a place on this
// plan to a place on a different image, so a line between them has no honest
// second end. They are drawn as a short stub ending in a label - "↓ B4" -
// which is the same convention the surveyors used in the node map itself,
// where those connectors run off the edge of the slide towards a word. The
// pay-off is that a staircase with *no* stub is visibly one that leads
// nowhere yet, which is the thing worth spotting.

import { fetchEdges, fetchFloorplan, fetchNodes, photoUrl } from "./api.js";
import { element, measureImage, pinRadius, toPixels } from "./mapView.js";

// How long a cross-floor stub is, as a share of the plan's longest side.
const STUB_LENGTH = 0.035;

/** Group places by the floor they are on, in a stable order. */
function floorsOf(nodes) {
  const floors = [];
  const seen = new Set();
  for (const node of nodes) {
    const key = `${node.building}|${node.floor}`;
    if (seen.has(key)) continue;
    seen.add(key);
    floors.push({ key, building: node.building, floor: node.floor });
  }
  floors.sort((a, b) =>
    a.building === b.building
      ? b.floor.localeCompare(a.floor)
      : a.building.localeCompare(b.building)
  );
  return floors;
}

function message(container, title, detail) {
  container.replaceChildren();
  const box = document.createElement("div");
  box.className = "floor-map-placeholder";

  const heading = document.createElement("p");
  heading.className = "floor-map-placeholder-title";
  heading.textContent = title;
  box.appendChild(heading);

  if (detail) {
    const note = document.createElement("p");
    note.className = "floor-map-placeholder-detail";
    note.textContent = detail;
    box.appendChild(note);
  }
  container.appendChild(box);
}

/**
 * Draw one floor.
 *
 * Returns a short report of what could not be drawn, which is the half of the
 * answer a list of pins cannot give: a place with no position and a link to
 * another floor both simply fail to appear otherwise.
 */
function drawFloor(svg, plan, size, { building, floor }, nodes, edges) {
  const here = nodes.filter(
    (node) => node.building === building && node.floor === floor
  );
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const placed = new Map();
  const unplaced = [];

  for (const node of here) {
    const at = toPixels(plan, node);
    if (at) placed.set(node.id, { ...at, node });
    else unplaced.push(node);
  }

  const onThisFloor = (id) => placed.has(id);
  const connected = new Set();
  const stubs = [];
  let drawnEdges = 0;

  for (const edge of edges) {
    const fromHere = onThisFloor(edge.from_id);
    const toHere = onThisFloor(edge.to_id);
    if (!fromHere && !toHere) continue;

    if (fromHere && toHere) {
      const a = placed.get(edge.from_id);
      const b = placed.get(edge.to_id);
      const line = element("line", {
        x1: a.x, y1: a.y, x2: b.x, y2: b.y,
        class: `floor-link${edge.stairs ? " stairs" : ""}${edge.lift ? " lift" : ""}`,
      });
      line.appendChild(element("title")).textContent =
        `${edge.label} · ${edge.walk_seconds}s${edge.covered ? "" : " · uncovered"}`;
      svg.appendChild(line);
      connected.add(edge.from_id).add(edge.to_id);
      drawnEdges += 1;
      continue;
    }

    // One end is on another plan. A line to it would have to stop somewhere
    // arbitrary, so it gets a stub towards the floor it leads to instead.
    const nearId = fromHere ? edge.from_id : edge.to_id;
    const farNode = byId.get(fromHere ? edge.to_id : edge.from_id);
    connected.add(nearId);
    stubs.push({ at: placed.get(nearId), to: farNode, edge });
  }

  // Fan the stubs out around their place, so two staircases from the same
  // landing do not draw on top of each other.
  const byPlace = new Map();
  for (const stub of stubs) {
    const list = byPlace.get(stub.at.node.id) ?? [];
    list.push(stub);
    byPlace.set(stub.at.node.id, list);
  }

  const reach = Math.max(size.width, size.height) * STUB_LENGTH;
  for (const list of byPlace.values()) {
    list.forEach((stub, index) => {
      const angle = -Math.PI / 2 + (index - (list.length - 1) / 2) * 0.7;
      const x = stub.at.x + Math.cos(angle) * reach;
      const y = stub.at.y + Math.sin(angle) * reach;

      const line = element("line", {
        x1: stub.at.x, y1: stub.at.y, x2: x, y2: y,
        class: `floor-link cross-floor${stub.edge.lift ? " lift" : ""}`,
      });
      line.appendChild(element("title")).textContent =
        `${stub.edge.label} · ${stub.edge.walk_seconds}s`;
      svg.appendChild(line);

      const label = element("text", {
        x, y,
        class: "floor-stub-label",
        "text-anchor": "middle",
        "font-size": pinRadius(size, 2.2),
      });
      const where = stub.to ? stub.to.floor || stub.to.building : "?";
      label.textContent = `${stub.edge.lift ? "lift" : "stairs"} ${where}`;
      svg.appendChild(label);
    });
  }

  for (const { x, y, node } of placed.values()) {
    const alone = !connected.has(node.id);
    const pin = element("circle", {
      cx: x, cy: y,
      r: pinRadius(size, alone ? 1.6 : 1.2),
      // A place nothing leads to cannot be routed to, so it is the one thing
      // on this map worth making impossible to miss.
      class: `floor-pin${alone ? " is-orphan" : ""}`,
    });
    pin.appendChild(element("title")).textContent =
      `${node.name} (${node.id})${alone ? " - nothing links to this" : ""}`;
    svg.appendChild(pin);

    const label = element("text", {
      x, y: y - pinRadius(size, 2),
      class: "floor-pin-label",
      "text-anchor": "middle",
      "font-size": pinRadius(size, 2),
    });
    label.textContent = node.name;
    svg.appendChild(label);
  }

  return {
    places: here.length,
    drawn: placed.size,
    unplaced,
    links: drawnEdges,
    crossFloor: stubs.length,
    orphans: [...placed.values()]
      .filter(({ node }) => !connected.has(node.id))
      .map(({ node }) => node),
  };
}

function reportOn(container, report) {
  const lines = [];
  lines.push(
    `${report.drawn} of ${report.places} places drawn · ${report.links} links · ` +
      `${report.crossFloor} to other floors`
  );
  if (report.unplaced.length > 0) {
    lines.push(
      `${report.unplaced.length} place(s) have no position, so cannot be drawn: ` +
        report.unplaced.map((node) => node.name).join(", ")
    );
  }
  if (report.orphans.length > 0) {
    lines.push(
      `${report.orphans.length} place(s) have nothing linking to them: ` +
        report.orphans.map((node) => node.name).join(", ")
    );
  }

  for (const text of lines) {
    const note = document.createElement("p");
    note.className = "floor-map-note";
    note.textContent = text;
    container.appendChild(note);
  }
}

/**
 * Draw every place and link on one floor into `container`.
 *
 * Reads the map fresh each time rather than taking a cached copy: this is an
 * editor's view, and its whole job is to show what the map says *now*.
 */
export async function showFloor(container, tabsElement, chosenKey = null) {
  message(container, "Loading the map…", null);

  let nodes;
  let edges;
  try {
    [nodes, edges] = await Promise.all([fetchNodes(), fetchEdges()]);
  } catch (error) {
    message(container, "Could not read the map", error.message);
    return;
  }

  const floors = floorsOf(nodes);
  if (floors.length === 0) {
    message(container, "There are no places on the map yet", null);
    return;
  }

  const chosen = floors.find((info) => info.key === chosenKey) ?? floors[0];

  tabsElement.replaceChildren();
  for (const info of floors) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "floor-tab";
    // So a refresh can put the reader back on the floor they were reading.
    button.dataset.key = info.key;
    button.classList.toggle("is-active", info.key === chosen.key);
    button.textContent = info.floor
      ? `${info.building} · ${info.floor}`
      : `${info.building} (outdoors)`;
    button.addEventListener("click", () => showFloor(container, tabsElement, info.key));
    tabsElement.appendChild(button);
  }
  tabsElement.hidden = false;

  let plan;
  try {
    plan = await fetchFloorplan(chosen.building, chosen.floor);
  } catch (error) {
    message(container, "Could not load the floorplan", error.message);
    return;
  }

  const where = chosen.floor
    ? `${chosen.building} · ${chosen.floor}`
    : chosen.building;

  if (!plan) {
    message(
      container,
      `No floorplan for ${where}`,
      "Upload one and the places on this floor can be drawn on it."
    );
    return;
  }
  if (!plan.is_calibrated) {
    message(
      container,
      `${where} has not been measured`,
      "The plan is uploaded, but nothing says where the map sits on it yet."
    );
    return;
  }

  let size;
  try {
    size = await measureImage(photoUrl(plan.url));
  } catch {
    message(container, "The floorplan image could not be loaded", plan.url);
    return;
  }

  const svg = element("svg", {
    class: "floor-map-svg",
    viewBox: `0 0 ${size.width} ${size.height}`,
    preserveAspectRatio: "xMidYMid meet",
  });
  svg.appendChild(
    element("image", {
      href: photoUrl(plan.url),
      x: 0, y: 0, width: size.width, height: size.height,
    })
  );

  const report = drawFloor(svg, plan, size, chosen, nodes, edges);
  container.replaceChildren(svg);
  reportOn(container, report);
}

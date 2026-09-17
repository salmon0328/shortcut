// The 3D campus: a MapLibre base map with deck.gl drawn into it.
//
// Loaded on demand by campus3d.js, never at startup: the two libraries are
// most of the app's weight, and a phone that only ever uses the flat plan
// should never download them.
//
// MapLibre draws the ground - streets, hills, every campus building raised
// from OpenStreetMap. deck.gl draws what is ours: the route as a real 3D line
// that climbs between floors, and a floor's plan lifted to that floor's
// height, which MapLibre cannot do (its images only lie on the ground).
//
// Everything this module knows about where things are comes from GET /geo.
// It does not decide anything about routes; it draws the one it is given.

import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { MapboxOverlay } from "@deck.gl/mapbox";
import { BitmapLayer, PathLayer, ScatterplotLayer } from "@deck.gl/layers";
import { TripsLayer } from "@deck.gl/geo-layers";

import { fetchFloorplan, photoUrl, sceneryUrl } from "./api.js";
import { getNodes } from "./data.js";
import { measureImage } from "./mapView.js";

// OpenFreeMap: OpenStreetMap vector tiles, free, no key.
const STYLE_URLS = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
// Mapzen's terrain, kept on AWS as open data. Used for shading only: the
// route and floors are placed by height above their building, and sinking
// them into real terrain would need every building's ground level too.
const TERRAIN_TILES =
  "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
const ATTRIBUTION =
  "Terrain: Mapzen, AWS Open Data · Buildings © OpenStreetMap contributors";

const HOME = { center: [103.6827, 1.3432], zoom: 17.2, pitch: 55, bearing: -20 };

// The route floats this far above its floor, so the plan beneath stays readable.
const ROUTE_LIFT_M = 1.0;
const PLAN_LIFT_M = 0.15;
const INTRO_MS = 1600;
const COMET_MS = 1800;

// Same sphere as shortcut/geo.py; see Transform.apply there.
const METRES_PER_DEGREE = (Math.PI * 6378137) / 180;

const reducedMotion = () =>
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** "light" or "dark", as the page is actually showing. */
export function themeName() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "light" || chosen === "dark") return chosen;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** A CSS colour token as deck.gl's [r, g, b, a]. */
function cssColor(name, alpha = 255) {
  const probe = document.createElement("span");
  probe.style.color = `var(${name})`;
  document.body.appendChild(probe);
  const [r, g, b] = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).map(Number);
  probe.remove();
  return [r, g, b, alpha];
}

const hex = ([r, g, b]) =>
  `#${[r, g, b].map((part) => Math.round(part).toString(16).padStart(2, "0")).join("")}`;

/**
 * Drawing units to [lon, lat]. A copy of shortcut.geo.Transform.apply; the
 * backend owns the numbers, this only repeats the arithmetic for a floorplan's
 * corners, which need the image's size and so cannot be worked out there.
 */
export function applyTransform(t, u, v) {
  const angle = (t.rotation_deg * Math.PI) / 180;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  const east = t.scale * (cos * u - sin * v);
  const south = t.scale * (sin * u + cos * v);
  const lat = t.origin_lat - south / METRES_PER_DEGREE;
  const lon =
    t.origin_lon + east / (METRES_PER_DEGREE * Math.cos((t.origin_lat * Math.PI) / 180));
  return [lon, lat];
}

function metresBetween(a, b) {
  const perLon = METRES_PER_DEGREE * Math.cos((a[1] * Math.PI) / 180);
  const dx = (b[0] - a[0]) * perLon;
  const dy = (b[1] - a[1]) * METRES_PER_DEGREE;
  return { flat: Math.hypot(dx, dy), dx, dy, dz: (b[2] ?? 0) - (a[2] ?? 0) };
}

/** Compass bearing from a to b, for pointing the camera the way you walk. */
function bearingOf(a, b) {
  const { dx, dy } = metresBetween(a, b);
  return (Math.atan2(dx, dy) * 180) / Math.PI;
}

const floorKey = (node) => (node ? `${node.building}|${node.floor}` : null);

/**
 * The route, one piece per step, in 3D.
 *
 * A step whose ends are not both on the globe is kept, marked unplaced, so
 * step numbers still line up with the list beside the map.
 */
function routePieces(route, places) {
  const byId = new Map(getNodes().map((node) => [node.id, node]));
  const pieces = [];
  let travelled = 0;

  (route.steps ?? []).forEach((step, index) => {
    const from = places[step.from_id];
    const to = places[step.to_id];
    const piece = {
      index,
      step,
      placed: Boolean(from && to),
      floorKey: floorKey(byId.get(step.to_id)),
      fromFloorKey: floorKey(byId.get(step.from_id)),
    };
    if (piece.placed) {
      const a = [from[0], from[1], from[2] + ROUTE_LIFT_M];
      const b = [to[0], to[1], to[2] + ROUTE_LIFT_M];
      const { flat, dz } = metresBetween(a, b);
      const length = Math.max(Math.hypot(flat, dz), 0.5);
      Object.assign(piece, {
        path: [a, b],
        start: travelled,
        length,
        timestamps: [travelled, travelled + length],
        vertical: flat < 2 && Math.abs(dz) > 0.5,
      });
      travelled += length;
    }
    pieces.push(piece);
  });

  return { pieces, total: travelled };
}

export class CampusMap {
  /**
   * @param {HTMLElement} container where the map draws
   * @param {object} geo the body of GET /geo
   * @param {{floorsEl?: HTMLElement, noteEl?: HTMLElement, compact?: boolean}} ui
   */
  constructor(container, geo, ui = {}) {
    this.container = container;
    this.geo = geo;
    this.ui = ui;

    this.route = null;
    this.pieces = [];
    this.total = 0;
    this.floorKey = null; // the floor being looked at, or null for all
    this.stepIndex = null; // null on the plan screen; a number while walking
    this.planLayer = null;
    this.planToken = 0;
    this.introStart = null;
    this.frame = null;

    this.map = new maplibregl.Map({
      container,
      style: STYLE_URLS[themeName()],
      ...HOME,
      maxPitch: 75,
      attributionControl: false,
      // One finger scrolls the page on a phone; two move the map.
      cooperativeGestures: window.matchMedia("(pointer: coarse)").matches,
    });
    this.map.addControl(
      new maplibregl.AttributionControl({ compact: true, customAttribution: ATTRIBUTION })
    );
    // Compact credits still open themselves on load, covering a third of a
    // phone-sized map; start them folded behind their (i) button instead.
    this.map.once("load", () => {
      container
        .querySelector(".maplibregl-compact-show")
        ?.classList.remove("maplibregl-compact-show");
    });
    if (!ui.compact) {
      this.map.addControl(
        new maplibregl.NavigationControl({ showZoom: false, visualizePitch: true }),
        "top-right"
      );
    }

    // interleaved: deck.gl draws inside MapLibre's own pass, so our layers
    // and the buildings share one depth buffer instead of stacking.
    this.overlay = new MapboxOverlay({ interleaved: true, layers: [] });
    this.map.addControl(this.overlay);

    this.map.on("style.load", () => this.decorateStyle());
    // The dark base style names a fill pattern its sprite does not have.
    // A transparent stand-in keeps the console quiet and changes nothing.
    this.map.on("styleimagemissing", (event) => {
      if (!this.map.hasImage(event.id)) {
        this.map.addImage(event.id, { width: 1, height: 1, data: new Uint8Array(4) });
      }
    });
    this.onTheme = () => this.map.setStyle(STYLE_URLS[themeName()]);
    document.addEventListener("shortcut:theme", this.onTheme);

    this.loaded = new Promise((resolve, reject) => {
      this.map.once("load", resolve);
      // No base map means no 3D map: let the caller fall back to the plan.
      const timer = setTimeout(() => reject(new Error("map timed out")), 15000);
      this.map.once("load", () => clearTimeout(timer));
      this.map.on("error", (event) => {
        if (!this.map.loaded() && /style/i.test(event.error?.message ?? "")) {
          clearTimeout(timer);
          reject(event.error);
        }
      });
    });

    if (new URLSearchParams(window.location.search).has("geo-debug")) {
      this.enableGeoDebug();
    }
  }

  // ------------------------------------------------------------------------
  // The ground: base map, hills and buildings
  // ------------------------------------------------------------------------

  /** Runs after every style load, including a theme change. */
  decorateStyle() {
    const map = this.map;
    const dark = themeName() === "dark";
    const layers = map.getStyle().layers;

    // The base map's own 3D buildings merge neighbours of equal height into
    // one shape, so ours could never be cut out of them. Campus buildings
    // come from /geo/scenery instead; see scripts/fetch_osm_buildings.py.
    for (const layer of layers) {
      if (layer.type === "fill-extrusion") map.removeLayer(layer.id);
    }
    const firstLabel = map.getStyle().layers.find((layer) => layer.type === "symbol")?.id;

    map.addSource("terrain-dem", {
      type: "raster-dem",
      tiles: [TERRAIN_TILES],
      tileSize: 256,
      encoding: "terrarium",
      maxzoom: 15,
    });
    map.addLayer(
      {
        id: "hillshade",
        type: "hillshade",
        source: "terrain-dem",
        paint: {
          "hillshade-exaggeration": dark ? 0.25 : 0.35,
          "hillshade-shadow-color": dark ? "#000000" : "#5b6378",
          "hillshade-highlight-color": dark ? "#262c4c" : "#ffffff",
        },
      },
      firstLabel
    );

    map.addSource("scenery", { type: "geojson", data: sceneryUrl() });
    map.addLayer({
      id: "scenery-3d",
      type: "fill-extrusion",
      source: "scenery",
      paint: {
        "fill-extrusion-color": dark ? "#262b45" : "#dfe2ea",
        "fill-extrusion-height": ["get", "height_m"],
        "fill-extrusion-base": ["get", "min_height_m"],
        "fill-extrusion-opacity": 0.85,
      },
    });

    map.addSource("campus", { type: "geojson", data: this.campusData() });
    map.addLayer({
      id: "campus-3d",
      type: "fill-extrusion",
      source: "campus",
      paint: {
        "fill-extrusion-color": [
          "case",
          ["get", "active"],
          hex(cssColor("--accent")),
          dark ? "#3a4270" : "#c3cbf2",
        ],
        "fill-extrusion-height": ["get", "height"],
        "fill-extrusion-base": 0,
        // See-through, so the route inside stays visible.
        "fill-extrusion-opacity": 0.45,
      },
    });

    this.render();
  }

  /**
   * Our buildings. The one whose floor is being looked at is cut down to
   * that floor, like a doll's house, so the plan laid on it can be seen.
   */
  campusData() {
    const [building, floor] = (this.floorKey ?? "|").split("|");
    const level = this.floorOf(building, floor);
    return {
      type: "FeatureCollection",
      features: (this.geo.buildings ?? [])
        .filter((entry) => entry.footprint)
        .map((entry) => ({
          type: "Feature",
          properties: {
            name: entry.name,
            active: entry.name === building,
            height:
              entry.name === building && level
                ? Math.max(level.elevation_m, 0.05)
                : entry.height_m,
          },
          geometry: { type: "Polygon", coordinates: [entry.footprint] },
        })),
    };
  }

  floorOf(building, floor) {
    const entry = (this.geo.buildings ?? []).find((b) => b.name === building);
    return entry?.floors.find((level) => level.floor === floor) ?? null;
  }

  // ------------------------------------------------------------------------
  // A floor's plan, lifted to its height
  // ------------------------------------------------------------------------

  async loadPlan(key) {
    const token = ++this.planToken;
    this.planLayer = null;
    if (!key) return;

    const [building, floor] = key.split("|");
    const level = this.floorOf(building, floor);
    if (!level?.plan) return;

    let plan;
    try {
      plan = await fetchFloorplan(building, floor);
    } catch {
      return; // storage unreachable: the route still draws
    }
    // A plan fitted to one image says nothing about its replacement.
    if (!plan || plan.id !== level.plan.floorplan_id) return;

    const url = photoUrl(plan.url);
    let size;
    try {
      size = await measureImage(url);
    } catch {
      return;
    }
    if (token !== this.planToken) return;

    const z = level.elevation_m + PLAN_LIFT_M;
    const corner = (u, v) => [...applyTransform(level.plan.transform, u, v), z];
    this.planLayer = new BitmapLayer({
      id: `plan-${key}`,
      image: url,
      // left-bottom, left-top, right-top, right-bottom - with heights.
      bounds: [
        corner(0, size.height),
        corner(0, 0),
        corner(size.width, 0),
        corner(size.width, size.height),
      ],
      opacity: 0.92,
      tintColor: themeName() === "dark" ? [205, 208, 222] : [255, 255, 255],
    });
    this.render();
  }

  // ------------------------------------------------------------------------
  // What the screens ask for
  // ------------------------------------------------------------------------

  /** Draw a whole route, and animate it in once. Returns how much could be placed. */
  showRoute(route) {
    this.route = route;
    const { pieces, total } = routePieces(route, this.geo.places ?? {});
    this.pieces = pieces;
    this.total = total;
    this.stepIndex = null;
    this.introStart = reducedMotion() ? null : performance.now();
    this.setFloor(null, { fly: false });
    this.fitTo(this.placedPoints(), { pitch: 55 });
    this.startLoop();
    return this.placement();
  }

  clearRoute() {
    this.route = null;
    this.pieces = [];
    this.total = 0;
    this.stepIndex = null;
    this.introStart = null;
    this.setFloor(null, { fly: false });
    this.map.easeTo({ ...HOME, duration: reducedMotion() ? 0 : 800 });
  }

  /** How much of the route is on the globe, and which buildings are missing. */
  placement() {
    const placed = this.pieces.filter((piece) => piece.placed).length;
    const byId = new Map(getNodes().map((node) => [node.id, node]));
    const missing = new Set();
    for (const piece of this.pieces) {
      if (piece.placed) continue;
      for (const id of [piece.step.from_id, piece.step.to_id]) {
        if (!(id in (this.geo.places ?? {}))) missing.add(byId.get(id)?.building ?? id);
      }
    }
    return { placed, total: this.pieces.length, missing: [...missing] };
  }

  /** The floors this route passes through that can be shown, in route order. */
  routeFloors() {
    const seen = new Set();
    const floors = [];
    for (const piece of this.pieces) {
      for (const key of [piece.fromFloorKey, piece.floorKey]) {
        if (!key || seen.has(key) || !piece.placed) continue;
        seen.add(key);
        const [building, floor] = key.split("|");
        if (this.floorOf(building, floor)) floors.push({ key, building, floor });
      }
    }
    return floors;
  }

  /** Look at one floor (or null for the whole route). */
  setFloor(key, { fly = true } = {}) {
    this.floorKey = key;
    this.map.getSource("campus")?.setData(this.campusData());
    this.loadPlan(key);
    this.renderFloorChips();
    this.render();

    if (fly && this.stepIndex === null) {
      const points = key
        ? this.pieces
            .filter((piece) => piece.placed && piece.floorKey === key)
            .flatMap((piece) => piece.path)
        : this.placedPoints();
      this.fitTo(points, { pitch: key ? 45 : 55 });
    }
  }

  /** Walking: bring one step forward, and follow it. */
  focusStep(index) {
    if (!this.route) return;
    this.stepIndex = index;
    this.introStart = null; // walking replaces the plan screen's drawing-in
    const piece = this.pieces[index];

    if (!piece) {
      // Past the last step: arrived. Show the whole way that was walked.
      this.setFloor(null, { fly: false });
      this.fitTo(this.placedPoints(), { pitch: 55 });
      this.note("");
      this.startLoop();
      return;
    }

    if (piece.floorKey !== this.floorKey) this.setFloor(piece.floorKey, { fly: false });
    else this.render();

    if (piece.placed) {
      this.note("");
      const [a, b] = piece.path;
      const bearing = piece.vertical ? this.map.getBearing() : bearingOf(a, b);
      // Gentler than the overview: a step that leads away from the camera
      // would otherwise shrink to a stub behind its own marker.
      this.fitTo(piece.path, { pitch: 45, bearing, maxZoom: 19.2 });
    } else {
      this.note("This stretch isn't on the 3D map yet.");
      this.fitTo(this.placedPoints(), { pitch: 55 });
    }
    this.startLoop();
  }

  note(text) {
    if (!this.ui.noteEl) return;
    this.ui.noteEl.textContent = text;
    this.ui.noteEl.hidden = !text;
  }

  placedPoints() {
    return this.pieces.filter((piece) => piece.placed).flatMap((piece) => piece.path);
  }

  // Overviews stop at 18: any closer and the camera sits inside a 30 m
  // building, looking at the route through its walls.
  fitTo(points, { pitch = 55, bearing = this.map.getBearing(), maxZoom = 18 } = {}) {
    if (points.length === 0) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const [lon, lat] of points) bounds.extend([lon, lat]);
    const compact = this.ui.compact;
    const camera = this.map.cameraForBounds(bounds, {
      padding: compact ? 40 : { top: 70, bottom: 60, left: 40, right: 40 },
      bearing,
      maxZoom,
    });
    if (!camera) return;
    this.map.easeTo({
      ...camera,
      pitch,
      bearing,
      duration: reducedMotion() ? 0 : 1100,
      essential: false,
    });
  }

  renderFloorChips() {
    const holder = this.ui.floorsEl;
    if (!holder) return;
    holder.replaceChildren();

    if (this.stepIndex !== null) {
      // Walking: just say which floor this is.
      const [building, floor] = (this.floorKey ?? "|").split("|");
      holder.hidden = !this.floorKey;
      if (this.floorKey) {
        const label = document.createElement("span");
        label.className = "map3d-floor-label";
        label.textContent = `${building} · ${floor}`;
        holder.appendChild(label);
      }
      return;
    }

    const floors = this.routeFloors();
    holder.hidden = floors.length === 0;
    if (floors.length === 0) return;

    const chip = (key, text) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "floor-tab";
      button.classList.toggle("is-active", key === this.floorKey);
      button.textContent = text;
      button.addEventListener("click", () => this.setFloor(key));
      holder.appendChild(button);
    };
    chip(null, "All floors");
    for (const info of floors) chip(info.key, `${info.building} · ${info.floor}`);
  }

  // ------------------------------------------------------------------------
  // deck.gl layers, and the animation that drives them
  // ------------------------------------------------------------------------

  render(now = performance.now()) {
    const accent = cssColor("--accent");
    const yes = cssColor("--yes");
    const danger = cssColor("--danger");
    const muted = cssColor("--muted");
    const glow = themeName() === "dark" ? [255, 255, 255, 235] : [255, 255, 255, 245];
    // Always on top: a route hidden behind a wall is no use to anyone.
    const onTop = { depthCompare: "always", depthWriteEnabled: false };
    const placed = this.pieces.filter((piece) => piece.placed);

    const layers = [];
    if (this.planLayer) layers.push(this.planLayer);

    // Widths in screen pixels: in metres a line that reads well across the
    // campus becomes a slab when the camera follows one short step.
    const lineProps = {
      getPath: (piece) => piece.path,
      widthUnits: "pixels",
      capRounded: true,
      jointRounded: true,
      billboard: true, // faces the camera, so a lift shaft still shows
      parameters: onTop,
    };

    if (this.stepIndex === null) {
      const drawing = this.introStart !== null && now - this.introStart < INTRO_MS;
      if (drawing) {
        const progress = (now - this.introStart) / INTRO_MS;
        layers.push(
          new TripsLayer({
            id: "route-intro",
            data: placed,
            getPath: (piece) => piece.path,
            getTimestamps: (piece) => piece.timestamps,
            getColor: accent,
            getWidth: 8,
            widthUnits: "pixels",
            capRounded: true,
            jointRounded: true,
            billboard: true,
            fadeTrail: false,
            trailLength: this.total + 1,
            currentTime: this.total * (1 - (1 - progress) ** 3),
            parameters: onTop,
          })
        );
      } else {
        this.introStart = null;
        layers.push(
          new PathLayer({
            id: "route",
            data: placed,
            ...lineProps,
            getColor: (piece) =>
              !this.floorKey || piece.floorKey === this.floorKey || piece.fromFloorKey === this.floorKey
                ? accent
                : [...accent.slice(0, 3), 70],
            getWidth: 7,
            updateTriggers: { getColor: [this.floorKey, accent] },
          })
        );
      }
    } else {
      const current = this.stepIndex;
      layers.push(
        new PathLayer({
          id: "route-done",
          data: placed.filter((piece) => piece.index < current),
          ...lineProps,
          getColor: [...yes.slice(0, 3), 170],
          getWidth: 5,
        }),
        new PathLayer({
          id: "route-ahead",
          data: placed.filter((piece) => piece.index > current),
          ...lineProps,
          getColor: [...muted.slice(0, 3), 130],
          getWidth: 4,
        })
      );

      const piece = placed.find((candidate) => candidate.index === current);
      if (piece) {
        const phase = reducedMotion() ? 1 : ((now % COMET_MS) / COMET_MS);
        layers.push(
          new PathLayer({
            id: "route-current",
            data: [piece],
            ...lineProps,
            getColor: accent,
            getWidth: 9,
          }),
          new TripsLayer({
            id: "route-comet",
            data: [piece],
            getPath: (item) => item.path,
            getTimestamps: (item) => item.timestamps,
            getColor: glow,
            getWidth: 5,
            widthUnits: "pixels",
            capRounded: true,
            billboard: true,
            fadeTrail: true,
            trailLength: piece.length * 0.6,
            currentTime: piece.start + phase * piece.length * 1.6,
            parameters: onTop,
          })
        );
      }
    }

    const ends = [];
    if (placed.length > 0) {
      const first = placed[0];
      const last = placed[placed.length - 1];
      if (first.index === 0) ends.push({ at: first.path[0], color: yes, radius: 9 });
      if (last.index === this.pieces.length - 1) {
        ends.push({ at: last.path[1], color: danger, radius: 9 });
      }
      const you = placed.find((candidate) => candidate.index === this.stepIndex);
      if (you) {
        // Where you are, and the end of this stretch - where to head for.
        ends.push({ at: you.path[0], color: accent, radius: 10, ring: true });
        if (you.index !== this.pieces.length - 1) {
          ends.push({ at: you.path[1], color: [255, 255, 255, 255], radius: 6, edge: accent });
        }
      }
    }
    layers.push(
      new ScatterplotLayer({
        id: "ends",
        data: ends,
        getPosition: (end) => end.at,
        getFillColor: (end) => end.color,
        getLineColor: (end) => end.edge ?? [255, 255, 255, 255],
        getRadius: (end) => end.radius,
        radiusUnits: "pixels",
        stroked: true,
        lineWidthUnits: "pixels",
        getLineWidth: (end) => (end.ring ? 4 : 2.5),
        billboard: true,
        parameters: onTop,
        updateTriggers: {
          getFillColor: [accent, yes, danger],
          getLineColor: [accent],
        },
      })
    );

    this.overlay.setProps({ layers });
  }

  startLoop() {
    if (this.frame !== null) return;
    const tick = (now) => {
      this.frame = null;
      // Hidden (another screen is showing): stop, and let resume() restart.
      if (!this.container.isConnected || this.container.offsetParent === null) {
        return;
      }
      this.render(now);
      const animating =
        this.introStart !== null ||
        (this.stepIndex !== null && this.pieces[this.stepIndex]?.placed && !reducedMotion());
      if (animating) this.frame = requestAnimationFrame(tick);
    };
    this.frame = requestAnimationFrame(tick);
  }

  /** The screen holding this map has just been shown. */
  resume() {
    this.map.resize();
    this.startLoop();
  }

  // ------------------------------------------------------------------------
  // ?geo-debug: tap to read a position, for scripts/georef_floor.py
  // ------------------------------------------------------------------------

  enableGeoDebug() {
    this.map.on("click", (event) => {
      const { lat, lng } = event.lngLat;
      const text = `${lat.toFixed(7)},${lng.toFixed(7)}`;
      navigator.clipboard?.writeText(text).catch(() => {});
      new maplibregl.Popup({ closeButton: false })
        .setLngLat(event.lngLat)
        .setText(`${text} (copied)`)
        .addTo(this.map);
    });
  }
}

# web/

See the top-level [README](../README.md) for what this project is and how to
run it.

```bash
npm install
npm run dev        # http://localhost:5173, expects the API on :8000
```

Vanilla JavaScript and Vite. **No framework and no UI library** — not as a
purity exercise, but because the whole front end is a search box, a list of
steps, a picture and a map, and every one of those is a dozen lines of DOM.
The source stays readable by somebody who has never seen this project.

The one exception is the **3D campus map**: MapLibre GL draws the base map
and the buildings, and deck.gl draws the route and lifts each floorplan to its
floor. A 3D map with hills is not a dozen lines of DOM. Both load only when a
map is first shown (`campus3d.js` imports `map3dView.js` on demand), so the
first screen is as light as it was, and anything that stops them — no WebGL,
no base map, a floor nobody has placed — falls back to the flat floorplan.

## What is where

| File | What it does |
|---|---|
| `index.html` | the whole markup, every screen |
| `src/main.js` | wiring: which screen is showing, and what talks to what |
| `src/api.js` | every call to the backend, in one place |
| `src/data.js` | the places and links the browser keeps in memory |
| `src/searchBox.js` | the type-ahead used by every place picker |
| `src/recent.js` | places this browser has routed between before |
| `src/theme.js` | the light / dark / follow-device switch |
| `src/plan.js` | a route, walked step by step |
| `src/report.js` | the "what's happening here?" form, including its photo |
| `src/mapView.js` | the flat floorplan panel drawn beside a route, and the fallback for 3D |
| `src/campus3d.js` | chooses 3D or flat, and keeps the plan panel and walk strip in step |
| `src/map3dView.js` | the 3D campus itself, loaded on demand |
| `src/floorMap.js` | the admin per-floor view of every place and link |
| `src/adminMap.js` | editing places, links and photos |
| `src/importReview.js` | reviewing what was read out of an uploaded PDF |
| `src/admin.js` | the reported-problem queue, and the Verifier's working |
| `src/pending.js` | changes sitting in the overrides file, not yet surveyed |
| `src/style.css` | every style, with the design tokens (light and dark) at the top |

## Conventions worth knowing

**The API base URL lives only in `api.js`.** Nothing else builds a URL, so
pointing the front end at a deployed backend is a one-line change.

**Colours are tokens, never literals.** Every colour is a variable on `:root`,
redefined for dark mode just below it, so a new rule that writes `#fff` is a
rule that breaks in the dark. Icons come from the one `<svg>` sprite at the top
of `index.html` and draw in `currentColor`.

**What the browser remembers stays in the browser.** `localStorage` holds the
theme (`shortcut-theme`), whether the splash has been seen
(`shortcut-seen-splash`), recent places (`shortcut-recent`), 3D or flat map
(`shortcut-map-mode`) and whether the walk map is folded away
(`shortcut-walk-map`). Every read is guarded, so a private window just starts
fresh.

**The 3D map's data is public, and credited on the map.** Base map:
OpenFreeMap (OpenStreetMap). Hill shading: Mapzen terrain on AWS Open Data.
Campus buildings: OpenStreetMap, via `GET /geo/scenery`. Add `?geo-debug` to
the page address and tapping the 3D map copies that spot's latitude and
longitude, for `scripts/georef_floor.py`.

**Nothing here decides anything about routing.** The browser asks for a route
and draws what comes back; it never scores, sorts or filters. Every rule about
what makes one way better than another lives in Python, where it is tested.

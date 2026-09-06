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
The build stays a single dependency and the source stays readable by somebody
who has never seen this project.

## What is where

| File | What it does |
|---|---|
| `index.html` | the whole markup, every screen |
| `src/main.js` | wiring: which screen is showing, and what talks to what |
| `src/api.js` | every call to the backend, in one place |
| `src/data.js` | the places and links the browser keeps in memory |
| `src/searchBox.js` | the type-ahead used by every place picker |
| `src/plan.js` | a route, walked step by step |
| `src/report.js` | the "what's happening here?" form, including its photo |
| `src/mapView.js` | the floorplan panel drawn beside a route |
| `src/floorMap.js` | the admin per-floor view of every place and link |
| `src/adminMap.js` | editing places, links and photos |
| `src/importReview.js` | reviewing what was read out of an uploaded PDF |
| `src/admin.js` | the reported-problem queue, and the Verifier's working |
| `src/pending.js` | changes sitting in the overrides file, not yet surveyed |
| `src/style.css` | every style, with the design tokens at the top |

## Two conventions worth knowing

**The API base URL lives only in `api.js`.** Nothing else builds a URL, so
pointing the front end at a deployed backend is a one-line change.

**Nothing here decides anything about routing.** The browser asks for a route
and draws what comes back; it never scores, sorts or filters. Every rule about
what makes one way better than another lives in Python, where it is tested.

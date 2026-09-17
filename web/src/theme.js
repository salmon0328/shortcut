// Light, dark, or whatever the device says.
//
// The choice lives in this browser only. index.html applies it before the
// first paint (so a dark-mode user never sees a white flash); this module owns
// the toggle and keeps "system" following the OS if it changes mid-visit.

const STORAGE_KEY = "shortcut-theme";
const MODES = ["system", "light", "dark"];

// What the browser chrome around the page should be, per resolved theme.
const CHROME = { light: "#1d2bb5", dark: "#0f1220" };

const LABELS = {
  system: "Theme: follows device",
  light: "Theme: light",
  dark: "Theme: dark",
};
const ICONS = { system: "i-monitor", light: "i-sun", dark: "i-moon" };

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

// Storage can be missing or throw (private windows, blocked site data), so
// every touch is guarded and the page works fine without it.
function readMode() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    return MODES.includes(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

function saveMode(mode) {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Still applied for this visit; it just will not be remembered.
  }
}

let mode = readMode();
let lastResolved = null;

function resolved() {
  if (mode !== "system") return mode;
  return darkQuery.matches ? "dark" : "light";
}

function applyTheme() {
  const root = document.documentElement;
  if (mode === "system") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = mode;
  }
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", CHROME[resolved()]);

  // The 3D map swaps its base map to match; nothing else needs telling.
  const now = resolved();
  if (now !== lastResolved) {
    lastResolved = now;
    document.dispatchEvent(new CustomEvent("shortcut:theme", { detail: now }));
  }
}

/** Wire a button that cycles system → light → dark. */
export function initThemeToggle(button) {
  const use = button.querySelector("use");

  function sync() {
    button.setAttribute("aria-label", LABELS[mode]);
    button.title = LABELS[mode];
    use?.setAttribute("href", `#${ICONS[mode]}`);
  }

  button.addEventListener("click", () => {
    mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
    saveMode(mode);
    applyTheme();
    sync();
  });

  darkQuery.addEventListener("change", applyTheme);
  applyTheme();
  sync();
}

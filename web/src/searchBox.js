// A text box that suggests places as you type.
//
// Used three times: the two route fields and the report form's location. The
// important part is that the typed text is never the answer. The box keeps the
// id of whatever was actually chosen, and forgets it the moment the text is
// edited, so the box can never read one place while meaning another.

import { getNodes } from "./data.js";

// Enough to scan without scrolling.
const MAX_SUGGESTIONS = 8;

/** How well a place matches what has been typed. Higher is better, -1 is no match. */
function matchScore(node, query) {
  if (!query) return 0; // nothing typed: everything is equally worth showing

  const name = node.name.toLowerCase();
  const everything =
    `${node.name} ${node.building} ${node.floor} ${node.id}`.toLowerCase();

  if (name.startsWith(query)) return 3;
  if (name.includes(query)) return 2;
  if (everything.includes(query)) return 1;
  return -1;
}

function suggestionsFor(query) {
  const cleaned = query.trim().toLowerCase();

  return getNodes()
    .map((node) => ({ node, score: matchScore(node, cleaned) }))
    .filter((entry) => entry.score >= 0)
    .sort((a, b) => b.score - a.score) // sort is stable, so ties keep graph order
    .slice(0, MAX_SUGGESTIONS)
    .map((entry) => entry.node);
}

const labelFor = (node) => `${node.name} (${node.building} · ${node.floor})`;

/**
 * Turn an input and a list element into a place picker.
 *
 * @param {HTMLInputElement} input
 * @param {HTMLElement} list
 * @param {(node: object|null) => void} [onChange] called whenever the choice changes
 * @returns {{selectedId: () => string|null, clear: () => void, select: (node) => void}}
 */
export function createSearchBox(input, list, onChange = () => {}) {
  let selectedId = null;
  let highlighted = -1;
  let shown = [];

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    highlighted = -1;
  }

  function choose(node) {
    selectedId = node.id;
    input.value = labelFor(node);
    close();
    onChange(node);
  }

  function render(nodes) {
    shown = nodes;
    list.replaceChildren();

    if (nodes.length === 0) {
      const empty = document.createElement("li");
      empty.className = "suggestion-empty";
      empty.textContent = "No matching place";
      list.appendChild(empty);
    }

    nodes.forEach((node, index) => {
      const item = document.createElement("li");
      item.className = "suggestion";
      item.setAttribute("role", "option");
      item.dataset.index = String(index);

      const name = document.createElement("span");
      name.className = "suggestion-name";
      name.textContent = node.name;

      const where = document.createElement("span");
      where.className = "suggestion-where";
      where.textContent = `${node.building} · Level ${node.floor}`;

      item.append(name, where);
      // mousedown, not click: it fires before the input loses focus, so the
      // blur handler cannot close the list out from under the tap.
      item.addEventListener("mousedown", (event) => {
        event.preventDefault();
        choose(node);
      });
      list.appendChild(item);
    });

    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function highlight(next) {
    if (shown.length === 0) return;
    highlighted = (next + shown.length) % shown.length;
    for (const item of list.querySelectorAll(".suggestion")) {
      item.classList.toggle(
        "is-highlighted",
        Number(item.dataset.index) === highlighted
      );
    }
  }

  input.addEventListener("input", () => {
    // Typing after choosing means the choice no longer matches the text.
    if (selectedId !== null) {
      selectedId = null;
      onChange(null);
    }
    render(suggestionsFor(input.value));
  });

  input.addEventListener("focus", () => render(suggestionsFor(input.value)));
  input.addEventListener("blur", () => close());

  input.addEventListener("keydown", (event) => {
    if (list.hidden && event.key === "ArrowDown") {
      render(suggestionsFor(input.value));
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight(highlighted + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(highlighted - 1);
    } else if (event.key === "Enter") {
      // Only swallow Enter when it is picking from the list; otherwise let it
      // submit the form as usual.
      if (!list.hidden && highlighted >= 0 && shown[highlighted]) {
        event.preventDefault();
        choose(shown[highlighted]);
      }
    } else if (event.key === "Escape") {
      close();
    }
  });

  return {
    selectedId: () => selectedId,
    select: (node) => choose(node),
    clear: () => {
      selectedId = null;
      input.value = "";
      close();
      onChange(null);
    },
  };
}

// A text box that suggests places as you type.
//
// Used three times: the two route fields and the report form's location. The
// important part is that the typed text is never the answer. The box keeps the
// id of whatever was actually chosen, and forgets it the moment the text is
// edited, so the box can never read one place while meaning another.

import { getNodes, placeWhere } from "./data.js";

// Enough to scan without scrolling, once something has been typed. With the
// box still empty there is nothing to rank by, so the cap would only ever
// keep whichever floor happens to come first in the graph file: an empty
// box shows every place instead, and the list scrolls.
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

function suggestionsFor(query, prefer) {
  const cleaned = query.trim().toLowerCase();

  return getNodes()
    .map((node) => {
      const score = matchScore(node, cleaned);
      // A half-point nudge: enough to float likely places to the top, never
      // enough to outrank a place whose name actually matches what was typed.
      return { node, score: score >= 0 && prefer(node) ? score + 0.5 : score };
    })
    .filter((entry) => entry.score >= 0)
    .sort((a, b) => b.score - a.score) // sort is stable, so ties keep graph order
    .slice(0, cleaned ? MAX_SUGGESTIONS : Infinity)
    .map((entry) => entry.node);
}

const labelFor = (node) => `${node.name} (${placeWhere(node)})`;

/**
 * Turn an input and a list element into a place picker.
 *
 * @param {HTMLInputElement} input
 * @param {HTMLElement} list
 * @param {(node: object|null) => void} [onChange] called whenever the choice changes
 * @param {{prefer?: (node: object) => boolean}} [options] `prefer` floats
 *   likely places to the top, which is what makes the list useful before
 *   anything has been typed. It is read on every keystroke, so it can depend
 *   on other fields the user is still filling in.
 * @returns {{selectedId: () => string|null, clear: () => void, select: (node) => void}}
 */
export function createSearchBox(input, list, onChange = () => {}, options = {}) {
  const prefer = options.prefer ?? (() => false);
  // An optional × beside the input. Shown only while there is something to
  // clear, so an empty box stays as plain as it looks.
  const clearButton = input.parentElement?.querySelector("[data-clear]") ?? null;
  let selectedId = null;
  let highlighted = -1;
  let shown = [];

  function syncClearButton() {
    if (clearButton) clearButton.hidden = input.value === "";
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    highlighted = -1;
  }

  function choose(node) {
    selectedId = node.id;
    input.value = labelFor(node);
    close();
    syncClearButton();
    onChange(node);
  }

  function clear() {
    selectedId = null;
    input.value = "";
    close();
    syncClearButton();
    onChange(null);
  }

  if (clearButton) {
    // mousedown, like the suggestions: it fires before the input loses
    // focus, so the list is not closed and reopened under the tap.
    clearButton.addEventListener("mousedown", (event) => {
      event.preventDefault();
      clear();
      input.focus();
    });
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
      where.textContent = node.floor
        ? `${node.building} · Level ${node.floor}`
        : node.building;

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
    syncClearButton();
    render(suggestionsFor(input.value, prefer));
  });

  input.addEventListener("focus", () => render(suggestionsFor(input.value, prefer)));
  input.addEventListener("blur", () => close());

  input.addEventListener("keydown", (event) => {
    if (list.hidden && event.key === "ArrowDown") {
      render(suggestionsFor(input.value, prefer));
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
    clear,
    // So a caller that could not resolve a place for the user can at least
    // put the cursor where they have to finish the job by hand.
    focus: () => input.focus(),
  };
}

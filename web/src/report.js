// The "what's happening here?" form.
//
// The place is always chosen from the map, never typed. That is what lets the
// backend group two people's reports about the same spot without having to
// reconcile spelling.

import { submitReport } from "./api.js";
import { edgesTouching, nodeName, otherEnd } from "./data.js";
import { createSearchBox } from "./searchBox.js";

const form = document.querySelector("#report-form");
const placeInput = document.querySelector("#report-place-input");
const placeSuggestions = document.querySelector("#report-place-suggestions");
const whereField = document.querySelector("#report-where-field");
const whereSelect = document.querySelector("#report-where");
const notesInput = document.querySelector("#report-notes");
const submitButton = document.querySelector("#report-submit");
const statusMessage = document.querySelector("#report-status");

function showStatus(text, kind) {
  statusMessage.textContent = text;
  statusMessage.className = `status ${kind}`;
  statusMessage.hidden = false;
}

function hideStatus() {
  statusMessage.hidden = true;
}

/**
 * Offer the place itself, plus each corridor leading away from it.
 *
 * A problem is either *at* somewhere ("the lobby is crowded") or *along* a
 * stretch ("the corridor to the courtyard is blocked"), and the backend needs
 * to know which, so this turns one chosen place into that choice.
 */
function fillWhereOptions(node) {
  whereSelect.replaceChildren();

  if (!node) {
    whereField.hidden = true;
    return;
  }

  const here = document.createElement("option");
  here.value = `node:${node.id}`;
  here.textContent = `At ${node.name}`;
  whereSelect.appendChild(here);

  for (const edge of edgesTouching(node.id)) {
    const option = document.createElement("option");
    option.value = `edge:${edge.id}`;
    option.textContent = `On the way to ${nodeName(otherEnd(edge, node.id))}`;
    whereSelect.appendChild(option);
  }

  whereField.hidden = false;
}

const placeBox = createSearchBox(placeInput, placeSuggestions, (node) => {
  hideStatus();
  fillWhereOptions(node);
});

/** Point the form at one specific corridor, used by "Report a problem". */
export function prefillFromStep(step) {
  resetForm();
  const option = document.createElement("option");
  option.value = `edge:${step.edge_id}`;
  option.textContent = `On the way to ${nodeName(step.to_id)}`;
  whereSelect.replaceChildren(option);
  whereField.hidden = false;
  placeInput.value = nodeName(step.from_id);
}

export function resetForm() {
  placeBox.clear();
  form.querySelectorAll('input[name="condition"]').forEach((input) => {
    input.checked = false;
  });
  notesInput.value = "";
  whereSelect.replaceChildren();
  whereField.hidden = true;
  hideStatus();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const where = whereSelect.value;
  const condition = form.querySelector('input[name="condition"]:checked');

  if (!where) {
    showStatus("Pick a location from the suggestions first.", "error");
    return;
  }
  if (!condition) {
    showStatus("Choose what is wrong.", "error");
    return;
  }

  const [targetKind, targetId] = where.split(/:(.+)/); // ids may contain ":"

  submitButton.disabled = true;
  submitButton.textContent = "Sending…";
  try {
    await submitReport({
      target_kind: targetKind,
      target_id: targetId,
      condition: condition.value,
      notes: notesInput.value.trim(),
    });
    resetForm();
    showStatus(
      "Thanks. Your report is waiting to be reviewed; the map has not changed yet.",
      "success"
    );
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Submit";
  }
});

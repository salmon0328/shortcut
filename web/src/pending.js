// The "not yet in the survey" list.
//
// Read-only on purpose. Everything here is sitting in graph_overrides.json,
// which is this machine's working state and is never committed. Moving any of
// it into data/campus_graph.json is done by running a script from a terminal,
// so that a change to a version controlled file gets reviewed as a git diff
// rather than happening on a click.

import { fetchPendingChanges } from "./api.js";

const list = document.querySelector("#pending-list");
const count = document.querySelector("#pending-count-map");
const statusMessage = document.querySelector("#pending-status");
const refreshButton = document.querySelector("#refresh-pending");

/** "x, y" from a field object, with values people can actually read. */
function describeFields(fields, only) {
  return only
    .map((key) => {
      const value = fields[key];
      if (value === true) return key;
      if (value === false) return `not ${key}`;
      return `${key} ${value}`;
    })
    .join(" · ");
}

function changeCard(change) {
  const card = document.createElement("article");
  card.className = "card pending-card";

  const head = document.createElement("div");
  head.className = "pending-head";

  const title = document.createElement("h3");
  title.textContent = change.label;
  head.appendChild(title);

  const tag = document.createElement("span");
  tag.className = `pending-tag ${change.change}`;
  tag.textContent = change.change === "added" ? `New ${change.kind}` : `Edited ${change.kind}`;
  head.appendChild(tag);

  card.appendChild(head);

  const id = document.createElement("p");
  id.className = "pending-id";
  id.textContent = change.id;
  card.appendChild(id);

  if (change.graduating_fields.length > 0) {
    const row = document.createElement("p");
    row.className = "pending-fields graduating";
    row.textContent = `Goes into the survey: ${describeFields(
      change.fields,
      change.graduating_fields
    )}`;
    card.appendChild(row);
  }

  if (change.live_fields.length > 0) {
    const row = document.createElement("p");
    row.className = "pending-fields live";
    row.textContent = `Stays here: ${describeFields(
      change.fields,
      change.live_fields
    )}`;
    card.appendChild(row);
  }

  return card;
}

export async function refreshPending() {
  try {
    const pending = await fetchPendingChanges();
    count.textContent = String(pending.total);
    statusMessage.hidden = true;
    list.replaceChildren();

    if (pending.total === 0) {
      const empty = document.createElement("p");
      empty.className = "status loading";
      empty.textContent =
        "Nothing changed since the survey. The map is exactly what is in campus_graph.json.";
      list.appendChild(empty);
      return;
    }

    // A one-line answer to "is there anything worth committing right now?"
    const summary = document.createElement("p");
    summary.className = "pending-summary";
    summary.textContent =
      `${pending.graduating} change(s) would go into the survey; ` +
      `${pending.live_only} would stay as live conditions.`;
    list.appendChild(summary);

    for (const change of pending.changes) {
      list.appendChild(changeCard(change));
    }
  } catch (error) {
    statusMessage.textContent = error.message;
    statusMessage.className = "status error";
    statusMessage.hidden = false;
  }
}

refreshButton.addEventListener("click", refreshPending);

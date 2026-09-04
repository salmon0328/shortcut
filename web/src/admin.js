// The pending-reports queue.
//
// Reports arrive grouped by the problem they describe, so one row can carry
// several people's confirmations. Approving a row applies it to the map for
// everyone; rejecting leaves the map exactly as surveyed.

import { fetchReportGroups, reviewReportGroup } from "./api.js";

const queue = document.querySelector("#report-queue");
const pendingCount = document.querySelector("#pending-count");
const statusMessage = document.querySelector("#admin-status");
const refreshButton = document.querySelector("#refresh-queue");

function showStatus(text, kind) {
  statusMessage.textContent = text;
  statusMessage.className = `status ${kind}`;
  statusMessage.hidden = false;
}

function makeButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function renderGroup(group) {
  const card = document.createElement("article");
  card.className = "card report-card";

  const heading = document.createElement("h3");
  heading.textContent = group.target_name;
  card.appendChild(heading);

  const tags = document.createElement("p");
  tags.className = "report-tags";

  const condition = document.createElement("span");
  condition.className = `condition-tag ${group.condition}`;
  condition.textContent = group.condition;
  tags.appendChild(condition);

  // An admin needs to know whether saying yes will close a path or only
  // add a warning, before they say it.
  const effect = document.createElement("span");
  effect.className = "effect-tag";
  effect.textContent = group.blocks_routes
    ? "Approving closes this to routing"
    : "Approving only adds a warning";
  tags.appendChild(effect);

  card.appendChild(tags);

  const confirmations = document.createElement("p");
  confirmations.className = "report-confirmations";
  confirmations.textContent =
    group.confirmations === 1
      ? "1 report"
      : `${group.confirmations} confirmations`;
  card.appendChild(confirmations);

  if (group.notes.length > 0) {
    const notes = document.createElement("ul");
    notes.className = "report-notes";
    for (const note of group.notes) {
      const item = document.createElement("li");
      item.textContent = note;
      notes.appendChild(item);
    }
    card.appendChild(notes);
  }

  const actions = document.createElement("div");
  actions.className = "report-actions";
  actions.append(
    makeButton("Approve", "approve", () => review(group.key, "approve")),
    makeButton("Reject", "secondary", () => review(group.key, "reject"))
  );
  card.appendChild(actions);

  return card;
}

async function review(key, action) {
  try {
    const result = await reviewReportGroup(key, action);
    const changed = result.routing_changed
      ? " The map has been updated."
      : " The map is unchanged.";
    showStatus(
      `${action === "approve" ? "Approved" : "Rejected"} ` +
        `${result.reports_updated} report(s).${changed}`,
      "success"
    );
  } catch (error) {
    showStatus(error.message, "error");
  }
  await refreshQueue();
}

export async function refreshQueue() {
  try {
    const groups = await fetchReportGroups();
    pendingCount.textContent = String(groups.length);
    queue.replaceChildren();

    if (groups.length === 0) {
      const empty = document.createElement("p");
      empty.className = "status loading";
      empty.textContent = "Nothing waiting to be reviewed.";
      queue.appendChild(empty);
      return;
    }

    for (const group of groups) {
      queue.appendChild(renderGroup(group));
    }
  } catch (error) {
    showStatus(error.message, "error");
  }
}

refreshButton.addEventListener("click", refreshQueue);

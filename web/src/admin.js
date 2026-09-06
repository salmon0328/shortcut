// The pending-reports queue.
//
// Reports arrive grouped by the problem they describe, so one row can carry
// several people's confirmations. Approving a row applies it to the map for
// everyone; rejecting leaves the map exactly as surveyed.

import {
  fetchPhotos,
  fetchReportGroups,
  photoUrl,
  reviewReportGroup,
  verifyAllReportGroups,
  verifyReportGroup,
} from "./api.js";

const queue = document.querySelector("#report-queue");
const pendingCount = document.querySelector("#pending-count");
const statusMessage = document.querySelector("#admin-status");
const refreshButton = document.querySelector("#refresh-queue");
const verifyAllButton = document.querySelector("#verify-all");

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

/**
 * A picture of the place, if the map has one.
 *
 * Reports do not carry their own photos yet, so the queue shows whatever
 * has already been photographed at that spot: enough for an admin to
 * recognise where a report is about. Starts as a placeholder and swaps in
 * the image once it is known to exist, so the card never waits on it.
 */
function thumbnailFor(group) {
  const empty = document.createElement("div");
  empty.className = "report-thumb-empty";
  empty.setAttribute("aria-hidden", "true");
  empty.innerHTML =
    '<svg viewBox="0 0 24 24" width="28" height="28">' +
    '<rect x="3" y="5" width="18" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<circle cx="8.5" cy="10" r="1.6" fill="currentColor"/>' +
    '<path d="M5 17l4.5-4.5 3 3 2.5-2.5L19 17" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>' +
    "</svg>";

  fetchPhotos(group.target_kind, group.target_id)
    .then((photos) => {
      if (!Array.isArray(photos) || photos.length === 0) return;
      const image = document.createElement("img");
      image.className = "report-thumb";
      image.src = photoUrl(photos[0].url);
      image.alt = group.target_name;
      image.loading = "lazy";
      empty.replaceWith(image);
    })
    .catch(() => {
      // No photo is not an error worth reporting; the placeholder stays.
    });

  return empty;
}

function renderGroup(group) {
  const card = document.createElement("article");
  card.className = "card report-card";

  card.appendChild(thumbnailFor(group));

  const body = document.createElement("div");
  body.className = "report-body";

  const heading = document.createElement("h3");
  heading.textContent = group.target_name;
  body.appendChild(heading);

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
  effect.textContent = group.blocks_routes ? "Closes to routing" : "Warning only";
  tags.appendChild(effect);

  body.appendChild(tags);

  const confirmations = document.createElement("p");
  confirmations.className = "report-confirmations";
  confirmations.textContent =
    group.confirmations === 1
      ? "1 report"
      : `${group.confirmations} confirmations`;
  body.appendChild(confirmations);

  const actions = document.createElement("div");
  actions.className = "report-actions";
  actions.append(
    makeButton("Approve", "approve", () => review(group.key, "approve")),
    makeButton("Reject", "secondary", () => review(group.key, "reject")),
    makeButton("Check it", "secondary", (event) => check(group.key, event.target))
  );
  body.appendChild(actions);

  card.appendChild(body);

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

/**
 * What the Verifier concluded, shown as its working rather than its answer.
 *
 * The weight, the bar it had to clear and what closing the place would cost
 * are all here because an administrator overruling this needs to see which
 * step they disagree with. A verdict that only said "escalated" would be
 * asking to be trusted, which is the opposite of the point.
 */
function verdictPanel(verdict) {
  const panel = document.createElement("div");
  panel.className = `verdict verdict-${verdict.action}`;

  const headline = document.createElement("p");
  headline.className = "verdict-headline";
  const badge = document.createElement("span");
  badge.className = "verdict-badge";
  badge.textContent = {
    approved: "Approved",
    rejected: "Rejected",
    escalated: "Left for you",
  }[verdict.action];
  const why = document.createElement("span");
  why.textContent = verdict.why;
  headline.append(badge, why);
  panel.appendChild(headline);

  const sums = document.createElement("p");
  sums.className = "verdict-sums";
  sums.textContent =
    `Corroboration ${verdict.weight} against a bar of ${verdict.threshold}` +
    (verdict.raised_because ? ` (raised from ${verdict.base_threshold})` : "") +
    ` · ${verdict.impact.describes}`;
  panel.appendChild(sums);

  if (verdict.reading.weights.length > 0) {
    const list = document.createElement("ul");
    list.className = "verdict-weights";
    for (const weight of verdict.reading.weights) {
      const item = document.createElement("li");
      const score = document.createElement("strong");
      score.textContent = weight.weight.toFixed(2);
      const reason = document.createElement("span");
      reason.textContent = weight.why;
      item.append(score, reason);
      list.appendChild(item);
    }
    panel.appendChild(list);
  }

  return panel;
}

async function check(key, button) {
  // A model call takes a second or two, and a button that looks idle while
  // it waits gets pressed again.
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Checking…";

  try {
    const verdict = await verifyReportGroup(key);
    if (verdict.applied) {
      // The row has left the queue, so there is nothing left to attach the
      // reasoning to - it goes in the status line instead.
      showStatus(`${verdict.action}: ${verdict.why}`, "success");
      await refreshQueue();
      return;
    }
    button.closest(".report-card").appendChild(verdictPanel(verdict));
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function checkEverything() {
  verifyAllButton.disabled = true;
  const label = verifyAllButton.textContent;
  verifyAllButton.textContent = "Checking…";

  try {
    const verdicts = await verifyAllReportGroups();
    const decided = verdicts.filter((v) => v.applied).length;
    const left = verdicts.length - decided;
    showStatus(
      verdicts.length === 0
        ? "Nothing waiting to check."
        : `Checked ${verdicts.length}: settled ${decided}, left ${left} for you.`,
      "success"
    );
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    verifyAllButton.disabled = false;
    verifyAllButton.textContent = label;
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
verifyAllButton?.addEventListener("click", checkEverything);

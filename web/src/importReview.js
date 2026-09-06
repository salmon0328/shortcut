// Reading a drawing, and deciding what the map should take from it.
//
// The one thing this screen exists to make true: nothing a machine read out
// of a PDF affects anybody until a person has looked at it. So a candidate
// here is inert - it routes nobody, appears in no search - and approving is
// the moment it becomes real, by going into the overrides file exactly as if
// it had been typed into the "Add a place" form.
//
// Every field is editable before that moment, because the drawing is reliable
// about geometry and unreliable about meaning: it knows where a place sits
// and how many seconds a walk takes, and it does not know what anywhere is
// called or whether a corridor is rained on.

import {
  approveAllImportCandidates,
  approveImportCandidate,
  fetchImportCandidates,
  importDrawings,
  rejectImportCandidate,
  updateImportCandidate,
} from "./api.js";
import { loadReferenceData } from "./data.js";

const fileInput = document.querySelector("#import-files");
const importButton = document.querySelector("#import-button");
const statusMessage = document.querySelector("#import-status");
const list = document.querySelector("#import-list");
const count = document.querySelector("#import-count");
const approveAllButton = document.querySelector("#approve-all");
const refreshButton = document.querySelector("#refresh-import");
const unsettledCard = document.querySelector("#import-unsettled-card");
const unsettledList = document.querySelector("#import-unsettled");
const unsettledCount = document.querySelector("#import-unsettled-count");

const NODE_TYPES = ["junction", "stairs", "lift", "entrance", "room", "bus_stop"];

function showStatus(text, kind = "") {
  statusMessage.textContent = text;
  statusMessage.className = kind ? `status ${kind}` : "status";
  statusMessage.hidden = !text;
}

/** A labelled input that reports what changed, and only what changed. */
function field(labelText, value, onCommit, options = {}) {
  const wrapper = document.createElement("label");
  wrapper.className = "import-field";

  const label = document.createElement("span");
  label.textContent = labelText;
  wrapper.appendChild(label);

  const input = options.choices
    ? document.createElement("select")
    : document.createElement("input");

  if (options.choices) {
    for (const choice of options.choices) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice.replace("_", " ");
      option.selected = choice === value;
      input.appendChild(option);
    }
  } else {
    input.type = options.type ?? "text";
    input.value = value ?? "";
    if (options.type === "number") input.step = options.step ?? "1";
  }

  // On change rather than on every keystroke: each commit is a request, and
  // one per letter typed would be a request per letter typed.
  input.addEventListener("change", () => {
    const raw = input.value.trim();
    if (raw === "") return; // the backend has no way to mean "blank"
    onCommit(options.type === "number" ? Number(raw) : raw);
  });

  wrapper.appendChild(input);
  return wrapper;
}

function checkbox(labelText, checked, onCommit) {
  const wrapper = document.createElement("label");
  wrapper.className = "choice";

  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.addEventListener("change", () => onCommit(input.checked));

  const text = document.createElement("span");
  text.textContent = labelText;

  wrapper.append(input, text);
  return wrapper;
}

/** Send one correction, and say so if it was refused. */
async function commit(candidate, changes) {
  try {
    await updateImportCandidate(candidate.id, changes);
    showStatus("Saved.", "success");
  } catch (error) {
    showStatus(error.message, "error");
    refresh(); // put the screen back to what the server actually holds
  }
}

function nodeFields(candidate) {
  const holder = document.createElement("div");
  holder.className = "import-fields";
  holder.append(
    field("Name", candidate.fields.name, (value) =>
      commit(candidate, { name: value })
    ),
    field("Building", candidate.fields.building, (value) =>
      commit(candidate, { building: value })
    ),
    field("Floor", candidate.fields.floor, (value) =>
      commit(candidate, { floor: value })
    ),
    field("Type", candidate.fields.type, (value) => commit(candidate, { type: value }), {
      choices: NODE_TYPES,
    })
  );
  return holder;
}

function edgeFields(candidate) {
  const holder = document.createElement("div");
  holder.className = "import-fields";
  holder.append(
    field(
      "Seconds",
      candidate.fields.walk_seconds,
      (value) => commit(candidate, { walk_seconds: value }),
      { type: "number" }
    ),
    field(
      "Metres",
      candidate.fields.distance_m,
      (value) => commit(candidate, { distance_m: value }),
      { type: "number", step: "0.1" }
    )
  );

  const flags = document.createElement("div");
  flags.className = "import-flags";
  flags.append(
    checkbox("Sheltered", candidate.fields.covered, (on) =>
      commit(candidate, { covered: on })
    ),
    checkbox("Stairs", candidate.fields.stairs, (on) =>
      commit(candidate, { stairs: on })
    ),
    checkbox("Lift", candidate.fields.lift, (on) => commit(candidate, { lift: on }))
  );

  holder.appendChild(flags);
  return holder;
}

/**
 * What was worked out about a floor's plan, as facts rather than boxes.
 *
 * Nothing here is editable. A scale is not a preference - it comes from the
 * links drawn across the floor - and a box inviting somebody to type a
 * different one would be inviting them to break the only thing that keeps the
 * image and the pins on it agreeing.
 */
function floorplanFacts(candidate) {
  const f = candidate.fields;
  const holder = document.createElement("div");
  holder.className = "import-facts";

  const [wide, tall] = f.measures_m ?? [0, 0];
  const rows = [
    ["Measures", `${wide} m x ${tall} m`],
    ["Image", `${f.width_px} x ${f.height_px} px`],
    ["Built from", `${f.made_of} crop(s) of the drawing`],
    ["Scale from", `${f.links_used} links, spread ${f.spread}x`],
  ];
  for (const [name, value] of rows) {
    const row = document.createElement("p");
    row.className = "import-fact";
    const label = document.createElement("span");
    label.textContent = name;
    const detail = document.createElement("strong");
    detail.textContent = value;
    row.append(label, detail);
    holder.appendChild(row);
  }
  return holder;
}

function note(text, kind) {
  const paragraph = document.createElement("p");
  paragraph.className = `import-note ${kind}`;
  paragraph.textContent = text;
  return paragraph;
}

function candidateCard(candidate) {
  const card = document.createElement("article");
  card.className = "card import-card";
  if (candidate.disagrees_with_survey) card.classList.add("is-disagreement");

  const head = document.createElement("div");
  head.className = "import-head";

  const title = document.createElement("h3");
  title.textContent = candidate.label;
  head.appendChild(title);

  const tag = document.createElement("span");
  tag.className = `pending-tag ${candidate.kind}`;
  tag.textContent =
    { node: "place", edge: "link", floorplan: "floorplan" }[candidate.kind] ??
    candidate.kind;
  head.appendChild(tag);

  card.appendChild(head);

  const where = document.createElement("p");
  where.className = "hint";
  where.textContent = `${candidate.target_id} · read from ${candidate.source}`;
  card.appendChild(where);

  // The two things the reviewer is here to settle, said plainly rather than
  // left for them to infer from a field that happens to look odd.
  if (candidate.name_is_a_stand_in) {
    card.appendChild(
      note("The drawing never gave this a name. The one above is a stand-in.", "warn")
    );
  }
  if (candidate.disagrees_with_survey) {
    card.appendChild(
      note(
        "Both ends are already surveyed, and the survey has no link between " +
          "them. Either this drawing is newer than the survey, or it is older " +
          "and its lettering means different places by the same names.",
        "warn"
      )
    );
  }
  if (candidate.kind === "floorplan" && !candidate.fields.well_conditioned) {
    card.appendChild(
      note(
        `The links on this floor disagree by ${candidate.fields.spread}x about ` +
          "how big it is, so no single scale fits them. Check the measurements " +
          "above look like the real building before approving.",
        "warn"
      )
    );
  }
  if (candidate.kind === "floorplan" && candidate.fields.replaces_existing) {
    card.appendChild(
      note(
        "This floor already has a plan. Approving stores this one alongside " +
          "it and the map starts using the newer one.",
        "info"
      )
    );
  }
  if (candidate.marks.length > 0) {
    card.appendChild(
      note(`The drawing wrote "${candidate.marks.join('", "')}" on this line.`, "info")
    );
  }

  if (candidate.kind === "floorplan") {
    card.appendChild(floorplanFacts(candidate));
  } else {
    card.appendChild(
      candidate.kind === "node" ? nodeFields(candidate) : edgeFields(candidate)
    );
  }

  const actions = document.createElement("div");
  actions.className = "import-actions";

  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "primary small";
  approve.textContent = "Approve";
  if (candidate.blocked_by.length > 0) {
    // Explained rather than merely disabled: a dead button with no reason is
    // indistinguishable from a broken one.
    approve.disabled = true;
    approve.title = "Approve the places at both ends first.";
    card.appendChild(
      note("Waiting on a place at one of its ends being approved first.", "info")
    );
  }
  approve.addEventListener("click", () => decide(candidate, approveImportCandidate));

  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "secondary small";
  reject.textContent = "Reject";
  reject.addEventListener("click", () => decide(candidate, rejectImportCandidate));

  actions.append(approve, reject);
  card.appendChild(actions);
  return card;
}

async function decide(candidate, action) {
  try {
    const result = await action(candidate.id);
    // The map changed underneath the rest of the app, so the place lists it
    // hands to every search box have to be re-read.
    if (result.approved) await loadReferenceData();
    showStatus(
      result.approved
        ? `Added ${result.target_id}. ${result.remaining} left to review.`
        : `Threw away ${result.target_id}. ${result.remaining} left to review.`,
      "success"
    );
  } catch (error) {
    showStatus(error.message, "error");
  }
  refresh();
}

function renderUnsettled(lines) {
  unsettledList.replaceChildren();
  unsettledCount.textContent = String(lines.length);
  unsettledCard.hidden = lines.length === 0;

  for (const line of lines) {
    const item = document.createElement("li");
    item.textContent = line;
    unsettledList.appendChild(item);
  }
}

export async function refresh() {
  try {
    const candidates = await fetchImportCandidates();
    count.textContent = String(candidates.length);
    approveAllButton.disabled = candidates.length === 0;

    list.replaceChildren();
    if (candidates.length === 0) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "Nothing waiting. Upload a drawing to read one.";
      list.appendChild(empty);
      return;
    }
    for (const candidate of candidates) list.appendChild(candidateCard(candidate));
  } catch (error) {
    showStatus(error.message, "error");
  }
}

importButton.addEventListener("click", async () => {
  const files = [...fileInput.files];
  if (files.length === 0) {
    showStatus("Choose one or more PDFs first.", "error");
    return;
  }

  importButton.disabled = true;
  importButton.textContent = "Reading…";
  showStatus("Reading the drawings…");

  try {
    const summary = await importDrawings(files);
    renderUnsettled(summary.unsettled);
    fileInput.value = "";
    showStatus(
      `Read ${summary.filenames.join(", ")}. ${summary.added} to review, ` +
        `${summary.already_known} already on the map` +
        (summary.already_waiting
          ? `, ${summary.already_waiting} already waiting.`
          : "."),
      "success"
    );
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    importButton.disabled = false;
    importButton.textContent = "Read drawings";
    refresh();
  }
});

approveAllButton.addEventListener("click", async () => {
  approveAllButton.disabled = true;
  showStatus("Approving everything…");
  try {
    const result = await approveAllImportCandidates();
    await loadReferenceData();

    // One candidate that could not be applied does not stop the rest, so the
    // report has to cover both halves. Saying only "added 28" would hide the
    // two still sitting in the list wondering why they are there.
    const added = `Added ${result.approved.length} to the map.`;
    if (result.skipped.length === 0) {
      showStatus(added, "success");
    } else {
      const reasons = result.skipped
        .map((item) => `${item.target_id}: ${item.reason}`)
        .join(" ");
      showStatus(
        `${added} ${result.skipped.length} could not be applied and are still ` +
          `waiting. ${reasons}`,
        "error"
      );
    }
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    refresh();
  }
});

refreshButton.addEventListener("click", refresh);

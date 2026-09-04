// The map-editing half of admin mode, across three panels: add a place, add a
// link, and edit something that already exists.
//
// Nothing here writes to the surveyed graph file. Every change goes to the
// backend, which records it as an override on top of the survey, so deleting
// that one file puts the map back exactly as it was measured.

import {
  addEdge,
  addNode,
  deleteAddition,
  deletePhoto,
  fetchPhotos,
  photoUrl,
  updateEdge,
  updateNode,
  uploadPhoto,
} from "./api.js";
import {
  edgesTouching,
  getNodes,
  loadReferenceData,
  nodeName,
  otherEnd,
} from "./data.js";
import { createSearchBox } from "./searchBox.js";

const statusMessage = document.querySelector("#map-status");

function showStatus(text, kind) {
  statusMessage.textContent = text;
  statusMessage.className = `status ${kind}`;
  statusMessage.hidden = false;
}

// --------------------------------------------------------------------------
// Choosing photos before uploading them
// --------------------------------------------------------------------------

/**
 * A list of chosen files, each with its own direction and caption.
 *
 * Per-file rather than one setting for the batch, because the usual job is
 * photographing a junction from every side at once: the whole point is that
 * each picture faces somewhere different.
 */
function createPhotoQueue(fileInput, listElement) {
  let entries = [];
  let facingChoices = [];

  function render() {
    listElement.replaceChildren();

    for (const [index, entry] of entries.entries()) {
      const row = document.createElement("div");
      row.className = "photo-queue-row";

      const name = document.createElement("span");
      name.className = "photo-queue-name";
      name.textContent = entry.file.name;

      const facing = document.createElement("select");
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "No particular direction";
      facing.appendChild(none);
      for (const nodeId of facingChoices) {
        const option = document.createElement("option");
        option.value = nodeId;
        option.textContent = `Towards ${nodeName(nodeId)}`;
        option.selected = nodeId === entry.facing;
        facing.appendChild(option);
      }
      facing.addEventListener("change", () => {
        entry.facing = facing.value;
      });

      const caption = document.createElement("input");
      caption.type = "text";
      caption.placeholder = "Caption (optional)";
      caption.value = entry.caption;
      caption.addEventListener("input", () => {
        entry.caption = caption.value;
      });

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        entries.splice(index, 1);
        render();
      });

      row.append(name, facing, caption, remove);
      listElement.appendChild(row);
    }
  }

  fileInput.addEventListener("change", () => {
    for (const file of fileInput.files) {
      entries.push({ file, facing: "", caption: "" });
    }
    // Clear the input so choosing the same file again still registers.
    fileInput.value = "";
    render();
  });

  return {
    /** Where the photos could be looking, e.g. the place's neighbours. */
    setFacingChoices(choices) {
      facingChoices = choices;
      render();
    },
    entries: () => entries,
    clear() {
      entries = [];
      render();
    },
    /** Upload everything queued. Returns how many worked and what failed. */
    async uploadAll(targetKind, targetId, location) {
      const failures = [];
      let uploaded = 0;

      for (const entry of entries) {
        try {
          await uploadPhoto({
            file: entry.file,
            targetKind,
            targetId,
            facing: entry.facing || null,
            location,
            caption: entry.caption,
          });
          uploaded += 1;
        } catch (error) {
          failures.push(`${entry.file.name}: ${error.message}`);
        }
      }
      return { uploaded, failures };
    },
  };
}

// --------------------------------------------------------------------------
// Add a place
// --------------------------------------------------------------------------

const addForm = document.querySelector("#add-node-form");
const newId = document.querySelector("#new-node-id");
const newName = document.querySelector("#new-node-name");
const newBuilding = document.querySelector("#new-node-building");
const newFloor = document.querySelector("#new-node-floor");
const newType = document.querySelector("#new-node-type");
const connectionRows = document.querySelector("#connection-rows");
const addConnectionButton = document.querySelector("#add-connection");

const newNodePhotos = createPhotoQueue(
  document.querySelector("#new-node-photos"),
  document.querySelector("#new-node-photo-queue")
);

// Each row keeps its own search box, so the chosen place is read from the box
// rather than scraped back out of the DOM.
let connectionRowState = [];
let nextRowNumber = 0;

/** A labelled number box that starts empty, so nobody guesses what it wants. */
function numberField(labelText, placeholder, step) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";

  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = step;
  input.placeholder = placeholder;
  input.required = true;
  input.id = `connection-${labelText.replace(/\W/g, "")}-${nextRowNumber}`;

  const label = document.createElement("label");
  label.textContent = labelText;
  label.htmlFor = input.id;

  wrapper.append(label, input);
  return { wrapper, input };
}

function checkboxField(labelText, checked) {
  const label = document.createElement("label");
  label.className = "choice";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  const text = document.createElement("span");
  text.textContent = labelText;
  label.append(input, text);
  return { label, input };
}

/** One "link it to somewhere" row in the add-a-place form. */
function addConnectionRow() {
  const rowNumber = (nextRowNumber += 1);

  const row = document.createElement("div");
  row.className = "connection-row";

  // -- which place --
  const searchField = document.createElement("div");
  searchField.className = "field";

  const searchLabel = document.createElement("label");
  searchLabel.textContent = "Link it to";
  searchLabel.htmlFor = `connection-target-${rowNumber}`;

  const combobox = document.createElement("div");
  combobox.className = "combobox";

  const input = document.createElement("input");
  input.type = "text";
  input.id = `connection-target-${rowNumber}`;
  input.placeholder = "Search for a place";
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-autocomplete", "list");

  const list = document.createElement("ul");
  list.className = "suggestions";
  list.id = `connection-suggestions-${rowNumber}`;
  list.setAttribute("role", "listbox");
  list.hidden = true;
  input.setAttribute("aria-controls", list.id);

  combobox.append(input, list);
  searchField.append(searchLabel, combobox);

  // Somewhere on the same floor of the same building is the likeliest thing
  // to join a new place to, so those come first before anything is typed.
  const box = createSearchBox(input, list, () => refreshNewNodeFacing(), {
    prefer: (node) =>
      node.building === newBuilding.value.trim() &&
      node.floor === newFloor.value.trim(),
  });

  // -- how far, how long --
  const numbers = document.createElement("div");
  numbers.className = "field-row";
  const distance = numberField("How far? (metres)", "e.g. 12", "0.1");
  const seconds = numberField("How long? (seconds)", "e.g. 9", "1");
  numbers.append(distance.wrapper, seconds.wrapper);

  // -- what kind of link --
  const flags = document.createElement("div");
  flags.className = "connection-flags";
  const covered = checkboxField("Sheltered", true);
  const stairs = checkboxField("Stairs", false);
  const lift = checkboxField("Lift", false);
  flags.append(covered.label, stairs.label, lift.label);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "secondary";
  remove.textContent = "Remove link";

  const state = {
    element: row,
    box,
    distance: distance.input,
    seconds: seconds.input,
    covered: covered.input,
    stairs: stairs.input,
    lift: lift.input,
  };

  remove.addEventListener("click", () => {
    row.remove();
    connectionRowState = connectionRowState.filter((entry) => entry !== state);
    refreshNewNodeFacing();
  });

  row.append(searchField, numbers, flags, remove);
  connectionRows.appendChild(row);
  connectionRowState.push(state);
  refreshNewNodeFacing();
}

/** The new place's photos can only face somewhere it is being joined to. */
function refreshNewNodeFacing() {
  newNodePhotos.setFacingChoices(
    connectionRowState.map((row) => row.box.selectedId()).filter(Boolean)
  );
}

function clearConnectionRows() {
  connectionRows.replaceChildren();
  connectionRowState = [];
}

addConnectionButton.addEventListener("click", addConnectionRow);

/**
 * Read the rows into what the API expects.
 *
 * Throws with a readable message rather than sending a half-filled row: an
 * empty number box reads as 0, which would quietly create a link that takes
 * no time to walk.
 */
function readConnections(nodeId) {
  return connectionRowState.map((row, index) => {
    const position = index + 1;
    const toId = row.box.selectedId();
    if (!toId) {
      throw new Error(`Link ${position}: pick a place from the suggestions.`);
    }
    if (row.distance.value.trim() === "" || row.seconds.value.trim() === "") {
      throw new Error(`Link ${position}: fill in the distance and the time.`);
    }

    return {
      from_id: nodeId,
      to_id: toId,
      distance_m: Number(row.distance.value),
      walk_seconds: Number(row.seconds.value),
      covered: row.covered.checked,
      stairs: row.stairs.checked,
      lift: row.lift.checked,
    };
  });
}

addForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const nodeId = newId.value.trim();

  let payload;
  try {
    payload = {
      id: nodeId,
      name: newName.value.trim(),
      building: newBuilding.value.trim(),
      floor: newFloor.value.trim(),
      type: newType.value,
      connections: readConnections(nodeId),
    };
  } catch (error) {
    showStatus(error.message, "error");
    return;
  }

  try {
    const result = await addNode(payload);
    await loadReferenceData(); // the map just changed under us

    // Photos can only be attached once the place exists to attach them to.
    const { uploaded, failures } = await newNodePhotos.uploadAll(
      "node",
      nodeId,
      ""
    );

    addForm.reset();
    clearConnectionRows();
    newNodePhotos.clear();
    addConnectionRow();

    const photoNote = uploaded ? ` ${uploaded} photo(s) uploaded.` : "";
    const problem = failures.length ? ` Some photos failed: ${failures.join("; ")}` : "";
    showStatus(
      `Added ${payload.name} with ${result.edge_ids.length} link(s). ` +
        `The map now has ${result.node_count} places and ` +
        `${result.edge_count} links.${photoNote}${problem}`,
      failures.length ? "error" : "success"
    );
  } catch (error) {
    showStatus(error.message, "error");
  }
});

// --------------------------------------------------------------------------
// Add a link
// --------------------------------------------------------------------------

const addEdgeForm = document.querySelector("#add-edge-form");
const linkDistance = document.querySelector("#link-distance");
const linkSeconds = document.querySelector("#link-seconds");

const linkFromBox = createSearchBox(
  document.querySelector("#link-from-input"),
  document.querySelector("#link-from-suggestions")
);
// Joining two places on the same floor is the common case, so once one end is
// chosen, places near it come first at the other.
const linkToBox = createSearchBox(
  document.querySelector("#link-to-input"),
  document.querySelector("#link-to-suggestions"),
  () => {},
  {
    prefer: (node) => {
      const fromId = linkFromBox.selectedId();
      if (!fromId) return false;
      const from = getNodes().find((candidate) => candidate.id === fromId);
      return Boolean(
        from && node.building === from.building && node.floor === from.floor
      );
    },
  }
);

addEdgeForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const fromId = linkFromBox.selectedId();
  const toId = linkToBox.selectedId();

  if (!fromId || !toId) {
    showStatus("Pick both places from the suggestions.", "error");
    return;
  }
  if (fromId === toId) {
    showStatus("A link has to join two different places.", "error");
    return;
  }
  if (linkDistance.value.trim() === "" || linkSeconds.value.trim() === "") {
    showStatus("Fill in the distance and the time.", "error");
    return;
  }

  try {
    const result = await addEdge({
      from_id: fromId,
      to_id: toId,
      distance_m: Number(linkDistance.value),
      walk_seconds: Number(linkSeconds.value),
      covered: document.querySelector("#link-covered").checked,
      stairs: document.querySelector("#link-stairs").checked,
      lift: document.querySelector("#link-lift").checked,
    });
    await loadReferenceData();
    const message =
      `Linked ${nodeName(fromId)} to ${nodeName(toId)}. ` +
      `The map now has ${result.edge_count} links.`;
    addEdgeForm.reset();
    linkFromBox.clear();
    linkToBox.clear();
    showStatus(message, "success");
  } catch (error) {
    showStatus(error.message, "error");
  }
});

// --------------------------------------------------------------------------
// Edit something that already exists
// --------------------------------------------------------------------------

const inspectTargetField = document.querySelector("#inspect-target-field");
const inspectTarget = document.querySelector("#inspect-target");
const inspectPanel = document.querySelector("#inspect-panel");
const inspectFields = document.querySelector("#inspect-fields");
const inspectSave = document.querySelector("#inspect-save");
const inspectDelete = document.querySelector("#inspect-delete");
const photoList = document.querySelector("#photo-list");
const photoForm = document.querySelector("#photo-form");
const photoLocation = document.querySelector("#photo-location");

const editPhotos = createPhotoQueue(
  document.querySelector("#photo-file"),
  document.querySelector("#photo-queue")
);

let selectedNode = null;
let target = null; // { kind, id, data }

function fillTargetOptions(node) {
  inspectTarget.replaceChildren();
  if (!node) {
    inspectTargetField.hidden = true;
    inspectPanel.hidden = true;
    return;
  }

  const here = document.createElement("option");
  here.value = `node:${node.id}`;
  here.textContent = `The place: ${node.name}`;
  inspectTarget.appendChild(here);

  for (const edge of edgesTouching(node.id)) {
    const option = document.createElement("option");
    option.value = `edge:${edge.id}`;
    option.textContent = `Link to ${nodeName(otherEnd(edge, node.id))}`;
    inspectTarget.appendChild(option);
  }

  inspectTargetField.hidden = false;
  showTarget();
}

const inspectBox = createSearchBox(
  document.querySelector("#inspect-place-input"),
  document.querySelector("#inspect-place-suggestions"),
  (node) => {
    selectedNode = node;
    fillTargetOptions(node);
  }
);

/** Render one editable field, remembering its key for saving. */
function field(key, labelText, value, type = "text") {
  const wrapper = document.createElement("div");
  wrapper.className = type === "checkbox" ? "choice" : "field";

  const label = document.createElement("label");
  const input = document.createElement("input");
  input.type = type;
  input.dataset.key = key;
  if (type === "checkbox") {
    input.checked = Boolean(value);
  } else {
    input.value = value ?? "";
    if (type === "number") input.step = "0.1";
  }

  if (type === "checkbox") {
    const text = document.createElement("span");
    text.textContent = labelText;
    label.append(input, text);
    wrapper.appendChild(label);
  } else {
    label.textContent = labelText;
    wrapper.append(label, input);
  }
  return wrapper;
}

function showTarget() {
  const [kind, id] = inspectTarget.value.split(/:(.+)/);
  const data =
    kind === "node"
      ? getNodes().find((node) => node.id === id)
      : edgesTouching(selectedNode.id).find((edge) => edge.id === id);

  target = { kind, id, data };
  inspectFields.replaceChildren();

  if (kind === "node") {
    inspectFields.append(
      field("name", "Name", data.name),
      field("building", "Building", data.building),
      field("floor", "Floor", data.floor),
      field("condition", "Condition (blank for none)", data.condition)
    );
  } else {
    inspectFields.append(
      field("distance_m", "Distance (m)", data.distance_m, "number"),
      field("walk_seconds", "Walk (sec)", data.walk_seconds, "number"),
      field("covered", "Sheltered", data.covered, "checkbox"),
      field("stairs", "Stairs", data.stairs, "checkbox"),
      field("lift", "Lift", data.lift, "checkbox"),
      field("blocked", "Closed", data.blocked, "checkbox"),
      field("condition", "Condition (blank for none)", data.condition)
    );
  }

  // A photo is only useful for directions if it says which way it looks, so
  // the choices are the places you could be heading from here.
  editPhotos.setFacingChoices(
    kind === "edge"
      ? [data.from_id, data.to_id]
      : edgesTouching(id).map((edge) => otherEnd(edge, id))
  );
  editPhotos.clear();
  refreshPhotos();
  inspectPanel.hidden = false;
}

inspectTarget.addEventListener("change", showTarget);

inspectSave.addEventListener("click", async () => {
  if (!target) return;

  const changes = {};
  for (const input of inspectFields.querySelectorAll("input")) {
    const key = input.dataset.key;
    if (input.type === "checkbox") {
      changes[key] = input.checked;
    } else if (input.type === "number") {
      changes[key] = Number(input.value);
    } else if (input.value.trim() !== "") {
      changes[key] = input.value.trim();
    }
    // A blank text box means "leave it alone": the API treats a missing field
    // as unchanged, which is what an empty input should mean here too.
  }

  try {
    if (target.kind === "node") {
      await updateNode(target.id, changes);
    } else {
      await updateEdge(target.id, changes);
    }
    await loadReferenceData();
    showStatus("Saved.", "success");
  } catch (error) {
    showStatus(error.message, "error");
  }
});

inspectDelete.addEventListener("click", async () => {
  if (!target) return;
  try {
    await deleteAddition(target.kind, target.id);
    await loadReferenceData();
    inspectBox.clear();
    inspectPanel.hidden = true;
    refreshMapEditor();
    showStatus("Removed.", "success");
  } catch (error) {
    showStatus(error.message, "error");
  }
});

async function refreshPhotos() {
  photoList.replaceChildren();
  try {
    const photos = await fetchPhotos(target.kind, target.id);
    if (photos.length === 0) {
      const empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "No photos yet.";
      photoList.appendChild(empty);
      return;
    }

    for (const photo of photos) {
      const card = document.createElement("figure");
      card.className = "photo-card";

      const image = document.createElement("img");
      image.src = photoUrl(photo.url);
      image.alt = photo.caption || photo.location || "Photo";
      card.appendChild(image);

      const caption = document.createElement("figcaption");
      const facing = photo.facing ? `Towards ${nodeName(photo.facing)}` : "No direction";
      caption.textContent = [facing, photo.location, photo.caption]
        .filter(Boolean)
        .join(" · ");
      card.appendChild(caption);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "Delete";
      remove.addEventListener("click", async () => {
        try {
          await deletePhoto(photo.id);
          refreshPhotos();
        } catch (error) {
          showStatus(error.message, "error");
        }
      });
      card.appendChild(remove);

      photoList.appendChild(card);
    }
  } catch (error) {
    showStatus(error.message, "error");
  }
}

photoForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!target || editPhotos.entries().length === 0) return;

  const { uploaded, failures } = await editPhotos.uploadAll(
    target.kind,
    target.id,
    photoLocation.value.trim()
  );

  editPhotos.clear();
  refreshPhotos();

  if (failures.length) {
    showStatus(`Uploaded ${uploaded}. Failed: ${failures.join("; ")}`, "error");
  } else {
    showStatus(`Uploaded ${uploaded} photo(s).`, "success");
  }
});

// --------------------------------------------------------------------------
// Keeping the pickers in step with the map
// --------------------------------------------------------------------------

/** Called when a map panel is opened.
 *
 * The search boxes read the place list live, so there is nothing to refill;
 * this only makes sure the add-a-place form starts with one link row ready.
 */
export function refreshMapEditor() {
  if (connectionRowState.length === 0) {
    addConnectionRow();
  }
}

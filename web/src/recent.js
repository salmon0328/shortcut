// Places this browser has recently routed to or from, newest first.
//
// Kept as node ids so a renamed place still shows its current name, and
// filtered against the loaded map so a removed place quietly drops out.
// Storage may be unavailable; then the list is simply empty.

import { getNodes } from "./data.js";

const STORAGE_KEY = "shortcut-recent";
const LIMIT = 5;

function readIds() {
  try {
    const ids = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function writeIds(ids) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Not remembered, which is all that is lost.
  }
}

/** Put these places at the front of the list, most important last. */
export function rememberPlaces(...ids) {
  let list = readIds();
  for (const id of ids) {
    list = [id, ...list.filter((other) => other !== id)];
  }
  writeIds(list.slice(0, LIMIT));
}

export function clearRecent() {
  writeIds([]);
}

/** The remembered places that still exist, as node objects. */
export function recentPlaces() {
  const byId = new Map(getNodes().map((node) => [node.id, node]));
  return readIds()
    .map((id) => byId.get(id))
    .filter(Boolean);
}

export function isRecent(node) {
  return readIds().includes(node.id);
}

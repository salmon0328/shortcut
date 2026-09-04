// The places and corridors the backend knows about, fetched once when the
// page opens. Nothing about the building is written down here: if the graph
// changes, this list changes with it.

import { fetchEdges, fetchNodes } from "./api.js";

let nodes = [];
let edges = [];
let nodesById = new Map();
let edgesById = new Map();

/** Fetch the map's places and corridors. Throws if the backend is unreachable. */
export async function loadReferenceData() {
  const [loadedNodes, loadedEdges] = await Promise.all([
    fetchNodes(),
    fetchEdges(),
  ]);

  if (!Array.isArray(loadedNodes) || loadedNodes.length === 0) {
    throw new Error("The backend returned no locations, so no route can be planned.");
  }

  nodes = loadedNodes;
  edges = Array.isArray(loadedEdges) ? loadedEdges : [];
  nodesById = new Map(nodes.map((node) => [node.id, node]));
  edgesById = new Map(edges.map((edge) => [edge.id, edge]));
}

export const getNodes = () => nodes;
export const getEdges = () => edges;

/** A place's readable name, falling back to its id if it is unknown. */
export const nodeName = (nodeId) => nodesById.get(nodeId)?.name ?? nodeId;

/** A place with its building and floor, for when the name alone is ambiguous. */
export function nodeLabel(nodeId) {
  const node = nodesById.get(nodeId);
  return node ? `${node.name} (${node.building} · ${node.floor})` : nodeId;
}

export const edgeLabel = (edgeId) => edgesById.get(edgeId)?.label ?? edgeId;

/** Every corridor, staircase or lift that touches a place. */
export const edgesTouching = (nodeId) =>
  edges.filter((edge) => edge.from_id === nodeId || edge.to_id === nodeId);

/** The place at the far end of an edge from where you are standing. */
export const otherEnd = (edge, nodeId) =>
  edge.from_id === nodeId ? edge.to_id : edge.from_id;

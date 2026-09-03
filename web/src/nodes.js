// The navigation points a user can pick from, copied from
// data/campus_graph.json.
//
// This list is duplicated on purpose to keep the first version simple: the
// backend has no endpoint that lists nodes yet. That means if a node is added,
// renamed or removed in the graph, this file must be updated by hand. The
// proper fix later is a GET /nodes endpoint that the page fetches on load.

export const NODES = [
  // Level B5
  { id: "Hive_B5_A", name: "Hive Level B5 Staircase 1", floor: "B5" },
  { id: "Hive_B5_B", name: "Hive Level B5 Lift", floor: "B5" },
  { id: "Hive_B5_C", name: "Hive Level B5 Staircase 3", floor: "B5" },
  { id: "Hive_B5_D", name: "Hive Level B5 Main Entarance", floor: "B5" },
  { id: "Hive_B5_E", name: "Hive Level B5 Staircase 2", floor: "B5" },
  { id: "Hive_B5_F", name: "Hive Level B5 Pick Lockers", floor: "B5" },
  { id: "Hive_B5_G", name: "Hive Level B5 Courtyard", floor: "B5" },
  { id: "Hive_B5_H", name: "Hive Level B5 Lift Lobby", floor: "B5" },
  { id: "Hive_B5_I", name: "Hive Level B5 Main Staircase", floor: "B5" },

  // Level B4
  { id: "Hive_B4_A", name: "Hive Level B4 Staircase 1", floor: "B4" },
  { id: "Hive_B4_B", name: "Hive Level B4 Lift", floor: "B4" },
  { id: "Hive_B4_C", name: "Hive Level B4 Staircase 3", floor: "B4" },
  { id: "Hive_B4_D", name: "Hive Level B4 Staircase 2", floor: "B4" },
  { id: "Hive_B4_E", name: "Hive Level B4 Side Entrance", floor: "B4" },
  { id: "Hive_B4_F", name: "Hive Level B4 Back Entrance", floor: "B4" },
  { id: "Hive_B4_G", name: "Hive Level B4 Main Staircase", floor: "B4" },
  { id: "Hive_B4_H", name: "Hive Level B4 Lift Lobby", floor: "B4" },
];

// The floors, in the order they should appear in the dropdowns.
export const FLOORS = ["B5", "B4"];

/**
 * Turn a node id such as "Hive_B5_A" into its readable name.
 * Falls back to the id itself if the graph knows a node this page does not.
 */
export function nodeName(nodeId) {
  const match = NODES.find((node) => node.id === nodeId);
  return match ? match.name : nodeId;
}

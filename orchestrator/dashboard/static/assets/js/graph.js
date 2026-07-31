/**
 * DAG layout: nodes and edges in, coordinates out.
 *
 * Pure arithmetic, no DOM. The renderer turns the result into SVG; keeping the
 * geometry separate is what lets the layout be tested for the properties that
 * actually matter — every dependency sits in an earlier column than its
 * dependent, nothing overlaps, and a cycle degrades instead of hanging.
 *
 * A cycle should be impossible: the API rejects one at registration. It is
 * handled anyway, because a layout routine that loops forever on bad input is a
 * frozen browser tab with no error message.
 */

/** Geometry, in SVG user units. */
export const METRICS = {
  nodeWidth: 150,
  nodeHeight: 44,
  columnGap: 56,
  rowGap: 14,
  padding: 12,
};

/**
 * Group nodes into columns so every node sits after all of its dependencies.
 *
 * @param {Array<{id: string, dependsOn?: string[]}>} nodes
 * @returns {{layers: string[][], cyclic: string[]}} The columns, and any nodes
 *   that could not be placed because they form a cycle.
 */
export function computeLayers(nodes) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const remaining = new Map(
    nodes.map((node) => [
      node.id,
      (node.dependsOn ?? []).filter((id) => byId.has(id) && id !== node.id),
    ]),
  );

  const layers = [];
  const placed = new Set();

  while (remaining.size) {
    const ready = [...remaining.entries()]
      .filter(([, deps]) => deps.every((id) => placed.has(id)))
      .map(([id]) => id)
      .sort();

    if (!ready.length) break; // everything left is in a cycle

    for (const id of ready) {
      remaining.delete(id);
      placed.add(id);
    }
    layers.push(ready);
  }

  return { layers, cyclic: [...remaining.keys()].sort() };
}

/**
 * Place nodes and route edges.
 *
 * @param {Array<{id: string, label?: string, sublabel?: string, tone?: string, dependsOn?: string[]}>} nodes
 * @param {object} [options]
 * @param {typeof METRICS} [options.metrics]
 * @returns {{nodes: any[], edges: any[], width: number, height: number, layers: string[][], cyclic: string[]}}
 */
export function layoutGraph(nodes, { metrics = METRICS } = {}) {
  if (!nodes?.length) {
    return { nodes: [], edges: [], width: 0, height: 0, layers: [], cyclic: [] };
  }

  const { layers, cyclic } = computeLayers(nodes);
  // A node caught in a cycle still has to appear somewhere; a trailing column
  // shows it exists and keeps the drawing finite.
  const columns = cyclic.length ? [...layers, cyclic] : layers;
  const byId = new Map(nodes.map((node) => [node.id, node]));

  const tallest = Math.max(...columns.map((column) => column.length), 1);
  const height =
    metrics.padding * 2 + tallest * metrics.nodeHeight + (tallest - 1) * metrics.rowGap;
  const width =
    metrics.padding * 2 +
    columns.length * metrics.nodeWidth +
    Math.max(0, columns.length - 1) * metrics.columnGap;

  const placed = [];
  const positions = new Map();

  columns.forEach((column, columnIndex) => {
    const columnHeight =
      column.length * metrics.nodeHeight + (column.length - 1) * metrics.rowGap;
    const top = (height - columnHeight) / 2;

    column.forEach((id, rowIndex) => {
      const source = byId.get(id) ?? { id };
      const x = metrics.padding + columnIndex * (metrics.nodeWidth + metrics.columnGap);
      const y = top + rowIndex * (metrics.nodeHeight + metrics.rowGap);
      const node = {
        id,
        label: source.label ?? id,
        sublabel: source.sublabel ?? '',
        tone: source.tone ?? 'idle',
        x,
        y,
        width: metrics.nodeWidth,
        height: metrics.nodeHeight,
        column: columnIndex,
        row: rowIndex,
        cyclic: cyclic.includes(id),
      };
      placed.push(node);
      positions.set(id, node);
    });
  });

  const edges = [];
  for (const node of nodes) {
    for (const dependency of node.dependsOn ?? []) {
      const from = positions.get(dependency);
      const to = positions.get(node.id);
      if (!from || !to || from === to) continue;
      edges.push({
        from: dependency,
        to: node.id,
        path: edgePath(from, to),
      });
    }
  }

  return { nodes: placed, edges, width, height, layers, cyclic };
}

/**
 * Route one edge as a cubic curve from the right of `from` to the left of `to`.
 *
 * Curves rather than straight lines because two edges arriving at the same node
 * from different rows are otherwise indistinguishable where they meet.
 *
 * @param {{x: number, y: number, width: number, height: number}} from
 * @param {{x: number, y: number, height: number}} to
 * @returns {string} An SVG path.
 */
export function edgePath(from, to) {
  const x1 = from.x + from.width;
  const y1 = from.y + from.height / 2;
  const x2 = to.x;
  const y2 = to.y + to.height / 2;
  const bend = Math.max(16, (x2 - x1) / 2);
  return `M ${round(x1)} ${round(y1)} C ${round(x1 + bend)} ${round(y1)}, ${round(x2 - bend)} ${round(y2)}, ${round(x2)} ${round(y2)}`;
}

/**
 * Build graph nodes from a workflow definition.
 *
 * @param {object} workflow A `/workflows/{name}` payload.
 * @returns {Array<object>}
 */
export function nodesFromWorkflow(workflow) {
  return (workflow?.steps ?? []).map((step) => ({
    id: step.name,
    label: step.title || step.name,
    sublabel: step.gates?.length ? `gates: ${step.gates.join(', ')}` : '',
    dependsOn: step.depends_on ?? [],
    tone: 'idle',
  }));
}

/**
 * Build graph nodes from a run's tasks, coloured by state.
 *
 * @param {any[]} tasks A `/runs/{id}/tasks` payload.
 * @param {(state: string) => string} toneFor Classifier, injected to keep this
 *   module free of the formatting vocabulary.
 * @returns {Array<object>}
 */
export function nodesFromTasks(tasks, toneFor) {
  return (tasks ?? []).map((task) => ({
    id: task.id,
    label: task.title,
    sublabel: task.attempts_made
      ? `${task.state} · attempt ${task.attempts_made}/${task.max_attempts}`
      : task.state,
    dependsOn: task.depends_on ?? [],
    tone: toneFor(task.state),
  }));
}

/**
 * Round to one decimal, so paths do not carry sixteen meaningless digits.
 *
 * @param {number} value
 * @returns {number}
 */
function round(value) {
  return Math.round(value * 10) / 10;
}

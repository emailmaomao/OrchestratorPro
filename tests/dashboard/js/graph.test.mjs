/**
 * Unit tests for the DAG layout.
 *
 * The properties that matter are structural: a dependency is always left of its
 * dependent, nothing overlaps, every edge connects two placed nodes, and a
 * cycle degrades rather than hanging the tab.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  METRICS,
  computeLayers,
  edgePath,
  layoutGraph,
  nodesFromTasks,
  nodesFromWorkflow,
} from '../../../orchestrator/dashboard/static/assets/js/graph.js';

const chain = [
  { id: 'a' },
  { id: 'b', dependsOn: ['a'] },
  { id: 'c', dependsOn: ['b'] },
];

const diamond = [
  { id: 'root' },
  { id: 'left', dependsOn: ['root'] },
  { id: 'right', dependsOn: ['root'] },
  { id: 'join', dependsOn: ['left', 'right'] },
];

describe('computeLayers', () => {
  it('puts a chain in one node per column', () => {
    assert.deepEqual(computeLayers(chain).layers, [['a'], ['b'], ['c']]);
  });

  it('puts independent nodes in the same column', () => {
    const { layers } = computeLayers([{ id: 'a' }, { id: 'b' }, { id: 'c' }]);
    assert.deepEqual(layers, [['a', 'b', 'c']]);
  });

  it('lays out a diamond in three columns', () => {
    assert.deepEqual(computeLayers(diamond).layers, [
      ['root'],
      ['left', 'right'],
      ['join'],
    ]);
  });

  it('reports nothing cyclic for a well-formed graph', () => {
    assert.deepEqual(computeLayers(diamond).cyclic, []);
  });

  it('sets aside a cycle instead of looping forever', () => {
    const { layers, cyclic } = computeLayers([
      { id: 'ok' },
      { id: 'a', dependsOn: ['b'] },
      { id: 'b', dependsOn: ['a'] },
    ]);
    assert.deepEqual(layers, [['ok']]);
    assert.deepEqual(cyclic, ['a', 'b']);
  });

  it('ignores a dependency on a node that is not present', () => {
    // A task list filtered by state can legitimately reference an absent task.
    const { layers, cyclic } = computeLayers([{ id: 'a', dependsOn: ['ghost'] }]);
    assert.deepEqual(layers, [['a']]);
    assert.deepEqual(cyclic, []);
  });

  it('ignores a self-dependency', () => {
    assert.deepEqual(computeLayers([{ id: 'a', dependsOn: ['a'] }]).layers, [['a']]);
  });

  it('handles no nodes at all', () => {
    assert.deepEqual(computeLayers([]).layers, []);
  });
});

describe('layoutGraph', () => {
  it('places every node', () => {
    const layout = layoutGraph(diamond);
    assert.equal(layout.nodes.length, 4);
    assert.deepEqual(
      layout.nodes.map((node) => node.id).sort(),
      ['join', 'left', 'right', 'root'],
    );
  });

  it('puts every dependency strictly left of its dependent', () => {
    const layout = layoutGraph(diamond);
    const at = new Map(layout.nodes.map((node) => [node.id, node]));

    for (const node of diamond) {
      for (const dependency of node.dependsOn ?? []) {
        assert.ok(
          at.get(dependency).x < at.get(node.id).x,
          `${dependency} should be left of ${node.id}`,
        );
      }
    }
  });

  it('never overlaps two nodes', () => {
    const layout = layoutGraph(diamond);
    for (const a of layout.nodes) {
      for (const b of layout.nodes) {
        if (a === b) continue;
        const apart =
          a.x + a.width <= b.x ||
          b.x + b.width <= a.x ||
          a.y + a.height <= b.y ||
          b.y + b.height <= a.y;
        assert.ok(apart, `${a.id} overlaps ${b.id}`);
      }
    }
  });

  it('keeps every node inside the reported canvas', () => {
    const layout = layoutGraph(diamond);
    for (const node of layout.nodes) {
      assert.ok(node.x >= 0 && node.y >= 0, `${node.id} is off the top-left`);
      assert.ok(node.x + node.width <= layout.width, `${node.id} runs off the right`);
      assert.ok(node.y + node.height <= layout.height, `${node.id} runs off the bottom`);
    }
  });

  it('draws one edge per declared dependency', () => {
    const layout = layoutGraph(diamond);
    assert.equal(layout.edges.length, 4);
    for (const edge of layout.edges) {
      assert.ok(edge.path.startsWith('M '), 'an edge should be a path');
    }
  });

  it('drops an edge whose endpoint is absent', () => {
    const layout = layoutGraph([{ id: 'a', dependsOn: ['ghost'] }]);
    assert.deepEqual(layout.edges, []);
  });

  it('still places a node caught in a cycle', () => {
    // The API rejects cycles, so this should be unreachable — but a layout
    // that silently drops nodes would hide the very thing worth seeing.
    const layout = layoutGraph([
      { id: 'a', dependsOn: ['b'] },
      { id: 'b', dependsOn: ['a'] },
    ]);
    assert.equal(layout.nodes.length, 2);
    assert.ok(layout.nodes.every((node) => node.cyclic));
  });

  it('returns an empty layout for no nodes', () => {
    assert.deepEqual(layoutGraph([]), {
      nodes: [],
      edges: [],
      width: 0,
      height: 0,
      layers: [],
      cyclic: [],
    });
    assert.equal(layoutGraph(undefined).width, 0);
  });

  it('carries labels, sublabels and tones through', () => {
    const layout = layoutGraph([
      { id: 'a', label: 'Build', sublabel: 'running', tone: 'busy' },
    ]);
    assert.equal(layout.nodes[0].label, 'Build');
    assert.equal(layout.nodes[0].sublabel, 'running');
    assert.equal(layout.nodes[0].tone, 'busy');
  });

  it('falls back to the id when there is no label', () => {
    assert.equal(layoutGraph([{ id: 'a' }]).nodes[0].label, 'a');
  });

  it('centres a short column against a tall one', () => {
    const layout = layoutGraph(diamond);
    const at = new Map(layout.nodes.map((node) => [node.id, node]));
    const middle = (node) => node.y + node.height / 2;

    assert.equal(middle(at.get('root')), layout.height / 2);
    assert.equal(middle(at.get('join')), layout.height / 2);
  });

  it('grows with the graph', () => {
    const small = layoutGraph(chain);
    const large = layoutGraph([...chain, { id: 'd', dependsOn: ['c'] }]);
    assert.ok(large.width > small.width);
  });

  it('honours injected metrics', () => {
    const layout = layoutGraph([{ id: 'a' }], {
      metrics: { ...METRICS, nodeWidth: 10, padding: 1 },
    });
    assert.equal(layout.width, 12);
  });
});

describe('edgePath', () => {
  it('runs from the right of one node to the left of the next', () => {
    const path = edgePath(
      { x: 0, y: 0, width: 100, height: 40 },
      { x: 200, y: 60, height: 40 },
    );
    assert.ok(path.startsWith('M 100 20'), path);
    assert.ok(path.endsWith('200 80'), path);
  });

  it('rounds, so a path is not sixteen meaningless digits', () => {
    const path = edgePath(
      { x: 0, y: 0, width: 33, height: 7 },
      { x: 100, y: 3, height: 7 },
    );
    assert.ok(!/\d\.\d\d/.test(path), path);
  });
});

describe('adapters', () => {
  it('builds nodes from a workflow definition', () => {
    const nodes = nodesFromWorkflow({
      steps: [
        { name: 'a', title: 'Design', depends_on: [], gates: [] },
        { name: 'b', title: '', depends_on: ['a'], gates: ['unit'] },
      ],
    });

    assert.deepEqual(nodes[0], {
      id: 'a',
      label: 'Design',
      sublabel: '',
      dependsOn: [],
      tone: 'idle',
    });
    assert.equal(nodes[1].label, 'b');
    assert.equal(nodes[1].sublabel, 'gates: unit');
  });

  it('tolerates a workflow with no steps', () => {
    assert.deepEqual(nodesFromWorkflow({}), []);
    assert.deepEqual(nodesFromWorkflow(null), []);
  });

  it('builds nodes from tasks, coloured by state', () => {
    const nodes = nodesFromTasks(
      [
        {
          id: 't1',
          title: 'Build',
          state: 'running',
          attempts_made: 2,
          max_attempts: 3,
          depends_on: [],
        },
      ],
      () => 'busy',
    );

    assert.equal(nodes[0].id, 't1');
    assert.equal(nodes[0].tone, 'busy');
    assert.equal(nodes[0].sublabel, 'running · attempt 2/3');
  });

  it('omits the attempt count before the first attempt', () => {
    const nodes = nodesFromTasks(
      [{ id: 't', title: 'x', state: 'pending', attempts_made: 0, max_attempts: 3, depends_on: [] }],
      () => 'idle',
    );
    assert.equal(nodes[0].sublabel, 'pending');
  });

  it('tolerates no tasks', () => {
    assert.deepEqual(nodesFromTasks(null, () => 'idle'), []);
  });
});

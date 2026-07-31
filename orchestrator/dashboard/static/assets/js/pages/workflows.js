/**
 * Workflows: the registry, and one definition's graph.
 *
 * This is the workflow graph viewer. It draws a definition *before* it has run,
 * which is the moment the drawing is worth most — an operator can see the shape
 * of the thing, where the parallelism is, and which steps carry gates, without
 * having to spend a run to find out.
 */

import { formatNumber, truncate } from '../format.js';
import { layoutGraph, nodesFromWorkflow } from '../graph.js';
import {
  badge,
  card,
  el,
  errorState,
  facts,
  graphView,
  link,
  mono,
  num,
  pageHeader,
  replace,
  table,
} from '../ui.js';

export const workflowsPage = {
  title: 'Workflows',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api }) {
    const workflows = await api.workflows();
    const fragment = document.createDocumentFragment();

    fragment.append(
      pageHeader('Workflows', {
        lede:
          'A registered definition is validated when it is submitted — an unknown ' +
          'dependency or a cycle is refused there, not discovered mid-run.',
      }),
    );

    fragment.append(
      table({
        columns: [
          'Name',
          'Goal',
          { label: 'Steps', class: 'num' },
          { label: 'Depth', class: 'num' },
          { label: 'Widest', class: 'num' },
          '',
        ],
        rows: workflows,
        empty: 'No workflows are registered. POST one to /workflows.',
        cell: (workflow) => [
          mono(workflow.name),
          truncate(workflow.goal, 50),
          num(formatNumber(workflow.steps.length)),
          num(formatNumber(workflow.depth)),
          num(formatNumber(workflow.max_width)),
          link(`workflows/${encodeURIComponent(workflow.name)}`, 'Open'),
        ],
      }),
    );

    return fragment;
  },
};

export const workflowDetailPage = {
  title: 'Workflow',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api, params, navigate }) {
    const workflow = await api.workflow(params.name);
    const fragment = document.createDocumentFragment();
    const container = el('div');

    const start = el('button', {
      text: 'Start a run',
      onClick: async () => {
        start.disabled = true;
        try {
          const started = await api.startWorkflow(workflow.name, {});
          navigate(`runs/${encodeURIComponent(started.id)}`);
        } catch (error) {
          start.disabled = false;
          replace(container, errorState(error));
        }
      },
    });

    fragment.append(
      pageHeader(workflow.name, {
        lede: workflow.goal,
        actions: [start, link('workflows', '← All workflows')],
      }),
    );
    fragment.append(container);

    fragment.append(
      card(null, [
        facts([
          ['Steps', formatNumber(workflow.steps.length)],
          ['Depth', `${workflow.depth} wave(s)`],
          ['Widest wave', `${workflow.max_width} step(s)`],
          ['Concurrency cap', formatNumber(workflow.max_concurrency)],
        ]),
      ]),
    );

    fragment.append(el('h2', { text: 'Graph' }));
    fragment.append(graphView(layoutGraph(nodesFromWorkflow(workflow))));

    fragment.append(el('h2', { text: 'Waves' }));
    fragment.append(
      table({
        columns: [{ label: 'Wave', class: 'num' }, 'Steps run together'],
        rows: workflow.layers.map((steps, index) => ({ index, steps })),
        empty: 'This workflow has no steps.',
        cell: (wave) => [num(wave.index + 1), wave.steps.join(', ')],
      }),
    );

    fragment.append(el('h2', { text: 'Steps' }));
    fragment.append(
      table({
        columns: ['Step', 'Role', 'Depends on', 'Gates', { label: 'Attempts', class: 'num' }, 'Condition'],
        rows: workflow.steps,
        empty: 'This workflow has no steps.',
        cell: (step) => [
          el('div', {}, [
            mono(step.name),
            step.title && step.title !== step.name
              ? el('div', { class: 'stat-note', text: step.title })
              : null,
          ]),
          badge(step.role, 'idle'),
          step.depends_on.length ? step.depends_on.join(', ') : '—',
          step.gates.length
            ? step.gates.map((gate) => badge(gate, 'warn'))
            : el('span', { class: 'lede', style: 'margin:0', text: 'none' }),
          num(step.max_attempts),
          step.condition ?? 'always',
        ],
      }),
    );

    return fragment;
  },
};

/**
 * Runs: the list, and one run in full.
 *
 * The detail view is where most of the milestone's features meet — the task
 * list, the workflow graph, and the log viewer are all views of the same run,
 * and splitting them across pages would make an operator navigate to answer
 * "which step is stuck and what did it say".
 *
 * It updates from the shared event stream rather than by polling: an event
 * arriving for this run triggers one refetch, so the page is live without
 * asking the server the same question four times a second.
 */

import {
  formatAge,
  formatCost,
  formatNumber,
  shortId,
  summarizeRun,
  toneForRunStatus,
  toneForTaskState,
  truncate,
} from '../format.js';
import { layoutGraph, nodesFromTasks } from '../graph.js';
import {
  badge,
  bar,
  card,
  el,
  emptyState,
  errorState,
  facts,
  graphView,
  link,
  logView,
  mono,
  num,
  pageHeader,
  replace,
  table,
} from '../ui.js';
import { toLogLines } from '../app.js';

export const runsPage = {
  title: 'Runs',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api, navigate }) {
    const runs = await api.runs({ limit: 100 });
    const fragment = document.createDocumentFragment();

    fragment.append(
      pageHeader('Runs', {
        lede:
          'Every run is reconstructed from its event log. A run that is not ' +
          'active has stopped, which is not the same as having succeeded.',
      }),
    );

    fragment.append(
      table({
        columns: ['Run', 'Goal', 'Status', { label: 'Tasks', class: 'num' }, ''],
        rows: runs,
        empty: 'No runs yet. Start one from the Workflows page.',
        cell: (run) => [
          mono(shortId(run.id)),
          truncate(run.goal, 60),
          badge(
            run.active ? 'running' : run.status,
            run.active ? 'busy' : toneForRunStatus(run.status),
          ),
          num(formatNumber(run.tasks)),
          link(`runs/${encodeURIComponent(run.id)}`, 'Open'),
        ],
      }),
    );

    if (runs.length) {
      fragment.append(
        el('p', {
          class: 'lede',
          text: `${runs.filter((run) => run.active).length} of ${runs.length} executing right now.`,
        }),
      );
    }
    void navigate;
    return fragment;
  },
};

export const runDetailPage = {
  title: 'Run',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api, params, hub, onCleanup, navigate }) {
    const runId = params.id;
    const container = el('div');

    /** Fetch everything this view shows, in one round. */
    const load = async () => {
      const [run, status, tasks] = await Promise.all([
        api.run(runId),
        api.runStatus(runId),
        api.tasks(runId),
      ]);
      return { run, status, tasks };
    };

    /** Repaint from a fresh snapshot. */
    const paint = (snapshot) => {
      replace(container, view(snapshot, { api, runId, navigate, refresh }));
    };

    let inFlight = false;
    /** Refetch and repaint, coalescing bursts of events into one round. */
    const refresh = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        paint(await load());
      } catch (error) {
        replace(container, errorState(error, { onRetry: refresh }));
      } finally {
        inFlight = false;
      }
    };

    paint(await load());

    // One refetch per event for this run. The alternative — polling — asks the
    // same question forever whether or not anything happened.
    let pending = null;
    const unsubscribe = hub.subscribe((event) => {
      if (event.run_id && event.run_id !== runId) return;
      clearTimeout(pending);
      pending = setTimeout(refresh, 120);
    });
    onCleanup(() => {
      clearTimeout(pending);
      unsubscribe();
    });

    return container;
  },
};

/**
 * Render one run.
 *
 * @param {{run: object, status: object, tasks: any[]}} snapshot
 * @param {object} actions
 * @returns {DocumentFragment}
 */
function view({ run, status, tasks }, { api, runId, navigate, refresh }) {
  const fragment = document.createDocumentFragment();
  const summary = summarizeRun(status);

  const cancel = el('button', {
    text: 'Cancel',
    dataset: { variant: 'danger' },
    disabled: !status.active,
    onClick: async () => {
      cancel.disabled = true;
      try {
        await api.cancelRun(runId);
      } catch (error) {
        cancel.disabled = false;
        cancel.title = error.message;
        return;
      }
      refresh();
    },
  });

  fragment.append(
    pageHeader(truncate(run.goal, 70) || 'Run', {
      actions: [
        badge(summary.label, summary.tone),
        cancel,
        el('button', { text: 'Refresh', onClick: refresh }),
        link('runs', '← All runs'),
      ],
    }),
  );

  fragment.append(
    card(null, [
      el('div', { class: 'toolbar' }, [
        bar(status.percent, summary.tone === 'bad' ? 'bad' : summary.tone === 'busy' ? 'busy' : 'ok'),
        el('span', { class: 'lede', style: 'margin:0', text: status.summary }),
      ]),
      facts([
        ['Run', mono(run.id)],
        ['Repository', run.repo_path ? mono(run.repo_path) : '—'],
        ['Status', `${run.status}${run.active ? ' (executing)' : ''}`],
        ['Created', run.created_at ? `${run.created_at} (${formatAge(run.created_at)})` : '—'],
        ['Finished', run.finished_at ?? '—'],
        ['Events', formatNumber(run.event_count)],
        ['Tool calls', formatNumber(run.tool_calls)],
        [
          // An estimated total must not read as a measurement (OP-004): the
          // tilde marks counts from a backend that cannot count exactly.
          run.usage.tokens_estimated ? 'Tokens (estimated)' : 'Tokens',
          `${run.usage.tokens_estimated ? '~' : ''}${formatNumber(run.usage.tokens_in)} in / ` +
            `${run.usage.tokens_estimated ? '~' : ''}${formatNumber(run.usage.tokens_out)} out`,
        ],
        ['Cost', formatCost(run.usage.cost_usd)],
      ]),
    ]),
  );

  fragment.append(el('h2', { text: 'Task graph' }));
  fragment.append(
    graphView(layoutGraph(nodesFromTasks(tasks, toneForTaskState)), {
      onSelect: (node) => {
        document.getElementById(`task-${node.id}`)?.scrollIntoView({ block: 'center' });
      },
    }),
  );

  fragment.append(el('h2', { text: 'Tasks' }));
  fragment.append(
    table({
      columns: ['Task', 'Title', 'State', { label: 'Attempts', class: 'num' }, 'Depends on'],
      rows: tasks,
      empty: 'This run has no tasks.',
      cell: (task) => [
        el('td', { id: `task-${task.id}` }, mono(shortId(task.id))),
        truncate(task.title, 50),
        badge(task.state, toneForTaskState(task.state)),
        num(`${task.attempts_made}/${task.max_attempts}`),
        task.depends_on.length
          ? task.depends_on.map((id) => mono(`${shortId(id)} `))
          : '—',
      ],
    }),
  );

  fragment.append(el('h2', { text: 'Log' }));
  fragment.append(logSection(api, runId));

  void navigate;
  return fragment;
}

/**
 * The run's log, read from the event store.
 *
 * Loaded separately from the rest of the view: a run with ten thousand events
 * should not make the task table wait, and an operator who never scrolls this
 * far should not pay for it.
 *
 * @param {import('../api.js').Api} api
 * @param {string} runId
 * @returns {HTMLElement}
 */
function logSection(api, runId) {
  const container = el('div', {}, emptyState('Loading the log…'));

  const load = async () => {
    try {
      const events = await api.log(runId, { limit: 500 });
      replace(container, logView(toLogLines(events), { empty: 'No events recorded.' }));
    } catch (error) {
      replace(container, errorState(error, { onRetry: load }));
    }
  };
  load();

  return container;
}

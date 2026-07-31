/**
 * Builds: history, and what a build would do before it does it.
 *
 * The plan preview is here because "what will this rebuild, and why" is the
 * question an incremental build system has to be able to answer out loud. A
 * build system that rebuilds the world and cannot say why is one people stop
 * trusting, and then stop using incrementally.
 *
 * Diagnostics are rendered per unit with their file and line. "The build failed"
 * is not actionable (FR-4.3), and neither is four hundred lines of log.
 */

import {
  formatDuration,
  formatNumber,
  summarizeBuild,
  toneForBuildStatus,
  truncate,
} from '../format.js';
import {
  badge,
  card,
  el,
  emptyState,
  errorState,
  facts,
  loading,
  mono,
  num,
  pageHeader,
  replace,
  table,
} from '../ui.js';

export const buildsPage = {
  title: 'Builds',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api, onCleanup }) {
    const fragment = document.createDocumentFragment();
    const history = el('div', {}, loading('Loading builds…'));

    fragment.append(
      pageHeader('Builds', {
        lede:
          'A unit is rebuilt when its sources change and when anything it ' +
          'depends on was rebuilt. A cache hit is verified against the disk ' +
          'before it is honoured.',
        actions: [
          el('button', {
            text: 'Clear cache',
            onClick: async () => {
              await api.clearBuildCache();
              load();
            },
          }),
        ],
      }),
    );

    fragment.append(planner(api));
    fragment.append(el('h2', { text: 'History' }));
    fragment.append(history);

    /** Reload the history table. */
    const load = async () => {
      try {
        replace(history, historyTable(await api.builds()));
      } catch (error) {
        replace(history, errorState(error, { onRetry: load }));
      }
    };
    await load();

    // A build started elsewhere should appear without a manual refresh, but a
    // build is not an event stream, so this is the one place polling is right.
    const timer = setInterval(load, 4000);
    onCleanup(() => clearInterval(timer));

    return fragment;
  },
};

/**
 * The build history table, with expandable detail.
 *
 * @param {any[]} builds
 * @returns {HTMLElement}
 */
function historyTable(builds) {
  if (!builds.length) {
    return emptyState('No builds yet. Plan one above, then run it.');
  }

  const rows = [...builds].reverse();
  const container = el('div');

  container.append(
    table({
      columns: [
        'Build',
        'Path',
        'Status',
        { label: 'Rebuilt', class: 'num' },
        { label: 'Cached', class: 'num' },
        { label: 'Duration', class: 'num' },
        '',
      ],
      rows,
      cell: (build) => {
        const summary = summarizeBuild(build);
        return [
          mono(build.id.replace(/^build_/, '')),
          truncate(build.path, 40),
          badge(summary.label, summary.tone),
          num(formatNumber(build.rebuilt.length)),
          num(formatNumber(build.cached.length)),
          num(formatDuration(build.duration_s)),
          build.units.length
            ? el('button', {
                text: 'Details',
                onClick: (event) => {
                  const detail = buildDetail(build);
                  event.target.replaceWith(detail);
                },
              })
            : '—',
        ];
      },
    }),
  );

  return container;
}

/**
 * One build's per-unit outcome.
 *
 * @param {object} build
 * @returns {HTMLElement}
 */
function buildDetail(build) {
  return el('div', {}, [
    table({
      columns: ['Unit', 'Status', { label: 'Duration', class: 'num' }, 'Artifacts'],
      rows: build.units,
      cell: (unit) => [
        mono(unit.unit),
        badge(unit.status, toneForBuildStatus(unit.status)),
        num(formatDuration(unit.duration_s)),
        unit.artifacts.length ? unit.artifacts.join(', ') : '—',
      ],
    }),
    ...build.units
      .filter((unit) => unit.diagnostics.length)
      .map((unit) =>
        el('div', {}, [
          el('h2', { text: `${unit.unit} — diagnostics` }),
          ...unit.diagnostics.map((diagnostic) =>
            el('div', { class: 'diag', dataset: { severity: diagnostic.severity } }, [
              el('div', { text: diagnostic.rendered }),
            ]),
          ),
        ]),
      ),
  ]);
}

/**
 * The plan-and-run panel.
 *
 * @param {import('../api.js').Api} api
 * @returns {HTMLElement}
 */
function planner(api) {
  const path = el('input', {
    type: 'text',
    placeholder: 'Project path',
    'aria-label': 'Project path',
    style: 'flex:1;min-width:240px',
  });
  const changed = el('input', {
    type: 'text',
    placeholder: 'Changed files (comma separated, optional)',
    'aria-label': 'Changed files',
    style: 'flex:1;min-width:240px',
  });
  const output = el('div');

  /** Collect the request body both buttons share. */
  const body = () => ({
    path: path.value.trim(),
    changed_paths: changed.value
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean),
  });

  const plan = el('button', {
    text: 'Plan',
    onClick: async () => {
      if (!path.value.trim()) {
        replace(output, emptyState('Give a project path first.'));
        return;
      }
      replace(output, loading('Planning…'));
      try {
        replace(output, planView(await api.planBuild(body())));
      } catch (error) {
        replace(output, errorState(error));
      }
    },
  });

  const run = el('button', {
    text: 'Build',
    onClick: async () => {
      if (!path.value.trim()) {
        replace(output, emptyState('Give a project path first.'));
        return;
      }
      replace(output, loading('Starting…'));
      try {
        const started = await api.startBuild(body());
        replace(
          output,
          el('p', { class: 'lede', text: `Started ${started.id}. It will appear below.` }),
        );
      } catch (error) {
        replace(output, errorState(error));
      }
    },
  });

  return card('Plan a build', [
    el('div', { class: 'toolbar' }, [path, changed, plan, run]),
    output,
  ]);
}

/**
 * Render a plan, reason by reason.
 *
 * @param {object} plan
 * @returns {HTMLElement}
 */
function planView(plan) {
  if (plan.empty) {
    return el('div', {}, [
      el('p', { class: 'lede', text: plan.summary }),
      facts([['Up to date', plan.cached.join(', ') || 'nothing']]),
    ]);
  }

  return el('div', {}, [
    el('p', { class: 'lede', text: plan.summary }),
    table({
      columns: ['Unit', 'Why'],
      rows: plan.units,
      cell: (unit) => [mono(unit.name), unit.why],
    }),
    facts([
      ['Waves', plan.layers.map((layer) => layer.join(', ')).join('  →  ')],
      ['Most parallel', `${plan.max_parallel} unit(s)`],
      ['Up to date', plan.cached.join(', ') || 'nothing'],
    ]),
  ]);
}

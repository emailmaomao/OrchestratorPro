/**
 * Metrics, configuration, and system status.
 *
 * Three pages, one module: each is a read of one or two endpoints, and
 * splitting them further would be filing rather than structure.
 *
 * The metrics are derived from what the API reports, not accumulated in the
 * browser. A counter that a tab has been incrementing since Tuesday is a
 * counter that disagrees with the log, and the log is the record.
 */

import {
  aggregateMetrics,
  formatDuration,
  formatNumber,
  formatRate,
  summarizeRun,
} from '../format.js';
import {
  badge,
  card,
  el,
  emptyState,
  facts,
  mono,
  num,
  pageHeader,
  stat,
  table,
} from '../ui.js';

/** How many runs the metrics page samples. */
const SAMPLE = 50;

export const metricsPage = {
  title: 'Metrics',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api }) {
    const [runs, builds] = await Promise.all([api.runs({ limit: SAMPLE }), api.builds()]);
    const statuses = await Promise.all(runs.map((run) => api.runStatus(run.id)));
    const metrics = aggregateMetrics({ runs, builds, statuses });

    const fragment = document.createDocumentFragment();
    fragment.append(
      pageHeader('Metrics', {
        lede:
          `Derived from the ${runs.length} most recent run(s) and every build this ` +
          'server has started. A rate over no samples is shown as “—”, not as zero.',
      }),
    );

    fragment.append(
      el('div', { class: 'grid' }, [
        stat('Runs', formatNumber(metrics.runs), `${metrics.activeRuns} executing`),
        stat(
          'Run success rate',
          formatRate(metrics.runSuccessRate),
          `over ${metrics.finishedRuns} finished`,
        ),
        stat('Tasks', formatNumber(metrics.tasks), `${metrics.taskFailures} failed`),
        stat('Builds', formatNumber(metrics.builds), formatDuration(metrics.buildSeconds)),
        stat(
          'Build cache hits',
          formatRate(metrics.cacheHitRate),
          `${metrics.unitsCached} cached, ${metrics.unitsRebuilt} rebuilt`,
        ),
      ]),
    );

    fragment.append(el('h2', { text: 'Runs in this sample' }));
    fragment.append(
      table({
        columns: [
          'Run',
          'Outcome',
          { label: 'Steps', class: 'num' },
          { label: 'Succeeded', class: 'num' },
          { label: 'Failed', class: 'num' },
          { label: 'Attempts', class: 'num' },
        ],
        rows: statuses,
        empty: 'No runs to measure yet.',
        cell: (status) => {
          const summary = summarizeRun(status);
          return [
            mono(status.id.replace(/^run_/, '').slice(-8)),
            badge(summary.label, summary.tone),
            num(formatNumber(status.total)),
            num(formatNumber(status.succeeded)),
            num(formatNumber(status.failed)),
            num(formatNumber(status.total ? status.succeeded + status.failed : 0)),
          ];
        },
      }),
    );

    return fragment;
  },
};

export const configPage = {
  title: 'Configuration',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api }) {
    const config = await api.config();
    const fragment = document.createDocumentFragment();

    fragment.append(
      pageHeader('Configuration', {
        lede:
          'The effective configuration, after defaults, the user file, and the ' +
          'repository file have been merged. Credentials are rejected at load ' +
          'time, so there is nothing here to redact.',
      }),
    );

    fragment.append(
      card('Sources', [
        config.sources.length
          ? el(
              'ul',
              {},
              config.sources.map((source) => el('li', {}, mono(source))),
            )
          : el('p', { class: 'lede', style: 'margin:0', text: 'Built-in defaults only.' }),
      ]),
    );

    for (const [section, values] of [
      ['Run', config.run],
      ['Agent', config.agent],
      ['Git', config.git],
      ['Gates', config.gates],
      ['API', config.api],
    ]) {
      fragment.append(card(section, facts(Object.entries(values).map(renderEntry))));
    }

    fragment.append(el('h2', { text: 'Providers' }));
    fragment.append(
      table({
        columns: ['Provider', 'Model', 'Effort', 'Thinking', { label: 'Max tokens', class: 'num' }],
        rows: Object.entries(config.providers).map(([name, values]) => ({ name, ...values })),
        empty: 'No provider blocks are configured; built-in defaults apply.',
        cell: (provider) => [
          mono(provider.name),
          mono(provider.model),
          provider.effort,
          provider.thinking,
          num(formatNumber(provider.max_tokens)),
        ],
      }),
    );

    fragment.append(el('h2', { text: 'Role overrides' }));
    fragment.append(
      table({
        columns: ['Role', 'Model', 'Effort'],
        rows: Object.entries(config.roles).map(([role, values]) => ({ role, ...values })),
        empty: 'No role overrides; every role uses its provider’s settings.',
        cell: (role) => [mono(role.role), role.model ?? 'inherited', role.effort ?? 'inherited'],
      }),
    );

    return fragment;
  },
};

export const statusPage = {
  title: 'Status',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api, hub, onCleanup }) {
    const [health, openapi] = await Promise.all([api.health(), api.openapi()]);
    const fragment = document.createDocumentFragment();

    fragment.append(
      pageHeader('System status', {
        lede: 'What this server is, and what it is currently able to do.',
      }),
    );

    fragment.append(
      el('div', { class: 'grid' }, [
        stat(
          'Server',
          badge(health.status, health.status === 'ok' ? 'ok' : 'warn'),
          `up ${formatDuration(health.uptime_s)}`,
        ),
        stat('Runs recorded', formatNumber(health.runs), `${health.active_runs} executing`),
        stat(
          'Execution',
          badge(
            health.execution_available ? 'available' : 'not configured',
            health.execution_available ? 'ok' : 'warn',
          ),
          health.execution_available
            ? 'runs can be started'
            : 'this server records and reports only',
        ),
        stat(
          'Event stream',
          badge(hub.connected ? 'connected' : 'disconnected', hub.connected ? 'ok' : 'warn'),
          'server-sent events',
        ),
      ]),
    );

    fragment.append(
      card('Build', [
        facts([
          ['API version', health.version],
          ['Database', mono(health.database)],
          ['Schema version', formatNumber(health.schema_version)],
          ['OpenAPI', `${openapi.openapi} · ${Object.keys(openapi.paths).length} paths`],
          ['Authentication', badge('none in this build', 'warn')],
        ]),
      ]),
    );

    fragment.append(el('h2', { text: 'Endpoints' }));
    fragment.append(endpointTable(openapi));

    // The stream pill is only as fresh as the last render; a page whose whole
    // job is to say whether things are working should not go stale on the desk.
    const timer = setInterval(async () => {
      try {
        await api.health();
      } catch {
        /* the health pill in the masthead reports this */
      }
    }, 10000);
    onCleanup(() => clearInterval(timer));

    return fragment;
  },
};

/**
 * Render the API surface from its own document.
 *
 * @param {object} openapi
 * @returns {HTMLElement}
 */
function endpointTable(openapi) {
  const rows = [];
  for (const [path, operations] of Object.entries(openapi.paths ?? {})) {
    for (const [method, operation] of Object.entries(operations)) {
      rows.push({
        method: method.toUpperCase(),
        path,
        summary: operation.summary ?? '',
        tag: (operation.tags ?? [])[0] ?? '',
      });
    }
  }
  rows.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method));

  if (!rows.length) return emptyState('The API reported no paths.');

  return table({
    columns: ['Method', 'Path', 'Section', 'Summary'],
    rows,
    cell: (row) => [
      badge(row.method, row.method === 'GET' ? 'idle' : 'busy'),
      mono(row.path),
      row.tag,
      row.summary,
    ],
  });
}

/**
 * Render one configuration entry.
 *
 * @param {[string, unknown]} entry
 * @returns {[string, any]}
 */
function renderEntry([key, value]) {
  if (value === null || value === undefined) return [key, '—'];
  if (typeof value === 'boolean') return [key, value ? 'yes' : 'no'];
  if (typeof value === 'number') return [key, formatNumber(value)];
  if (Array.isArray(value)) return [key, value.join(', ') || '—'];
  return [key, String(value)];
}

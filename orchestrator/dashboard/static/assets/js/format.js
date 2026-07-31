/**
 * Pure derivations: numbers and records in, strings and tones out.
 *
 * Nothing here touches the DOM or the network, which is what makes it testable
 * without a browser — and most of what the dashboard gets wrong would be wrong
 * here, in an off-by-one or a misread status, rather than in the markup.
 *
 * The tone vocabulary is small on purpose: `ok`, `bad`, `warn`, `busy`, `idle`.
 * `warn` exists so that a broken build tool and failing code do not both render
 * red — they mean different things (FR-4.4), and a UI that flattens them is a
 * UI that teaches its operator to stop reading.
 */

/** @typedef {'ok' | 'bad' | 'warn' | 'busy' | 'idle'} Tone */

/** Task states that mean work is happening right now. */
const ACTIVE_TASK_STATES = new Set(['running', 'gating']);

/** Task states that mean the task will make no further progress. */
const TERMINAL_TASK_STATES = new Set(['succeeded', 'blocked', 'abandoned']);

const TASK_TONES = {
  succeeded: 'ok',
  running: 'busy',
  gating: 'busy',
  ready: 'busy',
  retrying: 'warn',
  failed: 'bad',
  abandoned: 'bad',
  blocked: 'bad',
  pending: 'idle',
};

const RUN_TONES = {
  created: 'idle',
  running: 'busy',
  finished: 'ok',
  cancelled: 'warn',
};

const BUILD_TONES = {
  succeeded: 'ok',
  cached: 'ok',
  skipped: 'idle',
  running: 'busy',
  failed: 'bad',
  // A build tool that broke verified nothing. That is not the same as code
  // that failed to compile, and it must not read the same.
  errored: 'warn',
  timed_out: 'warn',
  blocked: 'idle',
};

/**
 * Classify a task state.
 *
 * @param {string} state
 * @returns {Tone}
 */
export function toneForTaskState(state) {
  return TASK_TONES[state] ?? 'idle';
}

/**
 * Classify a run status.
 *
 * @param {string} status
 * @returns {Tone}
 */
export function toneForRunStatus(status) {
  return RUN_TONES[status] ?? 'idle';
}

/**
 * Classify a build or unit status.
 *
 * @param {string} status
 * @returns {Tone}
 */
export function toneForBuildStatus(status) {
  return BUILD_TONES[status] ?? 'idle';
}

/**
 * Whether a task state means work is under way.
 *
 * @param {string} state
 * @returns {boolean}
 */
export function isActiveState(state) {
  return ACTIVE_TASK_STATES.has(state);
}

/**
 * Whether a task state means the task has stopped for good.
 *
 * @param {string} state
 * @returns {boolean}
 */
export function isTerminalState(state) {
  return TERMINAL_TASK_STATES.has(state);
}

/**
 * Render a duration in seconds for a human.
 *
 * Precision drops as the magnitude rises: milliseconds matter for a gate and
 * are noise for a run that took two hours.
 *
 * @param {number | null | undefined} seconds
 * @returns {string}
 */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—';
  const value = Math.abs(seconds);
  if (value < 1) return `${Math.round(value * 1000)}ms`;
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(value / 60);
  if (value < 3600) return `${minutes}m ${Math.round(value % 60)}s`;
  const hours = Math.floor(value / 3600);
  return `${hours}h ${Math.floor((value % 3600) / 60)}m`;
}

/**
 * Render a count with thousands separators.
 *
 * @param {number | null | undefined} value
 * @returns {string}
 */
export function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toLocaleString('en-US');
}

/**
 * Render a cost in dollars.
 *
 * `null` stays "not reported" rather than becoming `$0.00`: an unpriced model
 * that reports nothing is not a run that cost nothing.
 *
 * @param {number | null | undefined} usd
 * @returns {string}
 */
export function formatCost(usd) {
  if (usd === null || usd === undefined) return 'not reported';
  if (usd === 0) return '$0.00';
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/**
 * Render the clock part of an ISO timestamp.
 *
 * @param {string | null | undefined} iso
 * @returns {string}
 */
export function formatClock(iso) {
  if (!iso) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '—';
  return at.toISOString().slice(11, 19);
}

/**
 * Render an ISO timestamp as an age.
 *
 * @param {string | null | undefined} iso
 * @param {Date} [now] Injected so the result is testable.
 * @returns {string}
 */
export function formatAge(iso, now = new Date()) {
  if (!iso) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '—';
  const seconds = (now.getTime() - at.getTime()) / 1000;
  if (seconds < 0) return 'just now';
  if (seconds < 45) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

/**
 * Shorten an identifier for a dense table.
 *
 * The prefix is kept because it says what the thing *is*, and the tail because
 * that is the part that differs between two of them.
 *
 * @param {string | null | undefined} id
 * @returns {string}
 */
export function shortId(id) {
  if (!id) return '—';
  const [prefix, body] = id.includes('_') ? [id.slice(0, id.indexOf('_')), id.slice(id.indexOf('_') + 1)] : ['', id];
  if (body.length <= 8) return id;
  return prefix ? `${prefix}_…${body.slice(-6)}` : `…${body.slice(-6)}`;
}

/**
 * Truncate text, marking that something was removed.
 *
 * @param {string | null | undefined} text
 * @param {number} [max]
 * @returns {string}
 */
export function truncate(text, max = 80) {
  if (!text) return '';
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

/**
 * Clamp a percentage into the range a progress bar can draw.
 *
 * @param {number | null | undefined} percent
 * @returns {number}
 */
export function clampPercent(percent) {
  if (percent === null || percent === undefined || Number.isNaN(percent)) return 0;
  return Math.max(0, Math.min(100, percent));
}

/**
 * Summarize a run's status payload in one line.
 *
 * `complete` means every step reached a terminal state, which is not the same
 * as every step having worked; `healthy` is the one that says nothing failed.
 * Conflating them is the single most tempting mistake this view can make.
 *
 * @param {object} status A `/runs/{id}/status` payload.
 * @returns {{tone: Tone, label: string, detail: string}}
 */
export function summarizeRun(status) {
  if (!status) return { tone: 'idle', label: 'unknown', detail: '' };
  const detail = status.summary ?? '';
  if (status.active) return { tone: 'busy', label: 'running', detail };
  if (status.total === 0) return { tone: 'idle', label: 'empty', detail };
  if (!status.complete) return { tone: 'warn', label: 'stopped', detail };
  return status.healthy
    ? { tone: 'ok', label: 'succeeded', detail }
    : { tone: 'bad', label: 'failed', detail };
}

/**
 * Summarize a build report in one line.
 *
 * @param {object} report A `/builds/{id}` payload.
 * @returns {{tone: Tone, label: string, detail: string}}
 */
export function summarizeBuild(report) {
  if (!report) return { tone: 'idle', label: 'unknown', detail: '' };
  return {
    tone: toneForBuildStatus(report.status),
    label: report.status,
    detail: report.summary ?? '',
  };
}

/**
 * Render one event as a line of log.
 *
 * @param {object} event An event from the stream or the replay.
 * @returns {{time: string, kind: string, text: string, tone: Tone}}
 */
export function describeEvent(event) {
  const payload = event?.payload ?? {};
  const kind = event?.type ?? 'unknown';
  return {
    time: formatClock(event?.ts),
    kind,
    tone: toneForEventType(kind),
    text: eventText(kind, payload, event),
  };
}

/**
 * Classify an event type.
 *
 * @param {string} type
 * @returns {Tone}
 */
export function toneForEventType(type) {
  if (type.endsWith('.failed') || type.endsWith('.abandoned') || type.endsWith('.blocked')) {
    return 'bad';
  }
  if (type.endsWith('.succeeded') || type === 'run.finished') return 'ok';
  if (type.endsWith('_error') || type.endsWith('.cancelled')) return 'warn';
  if (type.endsWith('.started') || type.endsWith('.created')) return 'busy';
  return 'idle';
}

/**
 * Render the body of a log line for one event.
 *
 * @param {string} kind
 * @param {object} payload
 * @param {object} event
 * @returns {string}
 */
function eventText(kind, payload, event) {
  switch (kind) {
    case 'run.created':
      return payload.goal ? `goal: ${payload.goal}` : 'run declared';
    case 'run.finished':
      return `outcome: ${payload.outcome ?? 'unknown'}`;
    case 'run.cancelled':
      return payload.reason ?? 'cancelled';
    case 'task.created':
      return payload.title ? `${payload.title}` : shortId(event?.task_id);
    case 'task.started':
      return `attempt ${payload.attempt ?? '?'}`;
    case 'task.failed':
      return `attempt ${payload.attempt ?? '?'} — ${payload.error_code ?? 'failed'}`;
    case 'task.abandoned':
    case 'task.blocked':
      return payload.reason ?? kind;
    case 'gate.evaluated':
      return `${payload.gate ?? 'gate'}: ${payload.verdict ?? '?'}`;
    case 'tool.called':
      return payload.tool ?? 'tool';
    case 'build.started':
      return `${(payload.units ?? []).length} unit(s) to rebuild`;
    case 'build.unit_finished':
      return `${payload.unit ?? '?'}: ${payload.status ?? '?'}`;
    case 'build.finished':
      return `outcome: ${payload.outcome ?? 'unknown'}`;
    default: {
      const keys = Object.keys(payload);
      if (!keys.length) return '';
      return truncate(keys.map((key) => `${key}=${renderValue(payload[key])}`).join(' '), 120);
    }
  }
}

/**
 * Render one payload value compactly.
 *
 * @param {unknown} value
 * @returns {string}
 */
function renderValue(value) {
  if (Array.isArray(value)) return `[${value.length}]`;
  if (value && typeof value === 'object') return '{…}';
  return String(value);
}

/**
 * Roll a set of runs and builds into the numbers the metrics page shows.
 *
 * Counts are derived rather than accumulated, so they cannot drift from the
 * records they describe.
 *
 * @param {object} input
 * @param {any[]} [input.runs] `/runs` summaries.
 * @param {any[]} [input.builds] `/builds` reports.
 * @param {any[]} [input.statuses] `/runs/{id}/status` payloads.
 * @returns {object}
 */
export function aggregateMetrics({ runs = [], builds = [], statuses = [] } = {}) {
  const finished = statuses.filter((status) => !status.active && status.total > 0);
  const succeeded = finished.filter((status) => status.complete && status.healthy);
  const tasks = statuses.reduce((total, status) => total + (status.total ?? 0), 0);
  const failures = statuses.reduce((total, status) => total + (status.failed ?? 0), 0);

  const finishedBuilds = builds.filter((build) => build.status !== 'running');
  const buildSeconds = finishedBuilds.reduce(
    (total, build) => total + (build.duration_s ?? 0),
    0,
  );
  const rebuilt = finishedBuilds.reduce(
    (total, build) => total + (build.rebuilt?.length ?? 0),
    0,
  );
  const cached = finishedBuilds.reduce(
    (total, build) => total + (build.cached?.length ?? 0),
    0,
  );

  return {
    runs: runs.length,
    activeRuns: runs.filter((run) => run.active).length,
    finishedRuns: finished.length,
    runSuccessRate: finished.length ? succeeded.length / finished.length : null,
    tasks,
    taskFailures: failures,
    builds: finishedBuilds.length,
    buildSeconds,
    unitsRebuilt: rebuilt,
    unitsCached: cached,
    cacheHitRate: rebuilt + cached ? cached / (rebuilt + cached) : null,
  };
}

/**
 * Render a ratio as a percentage, or a dash when there is nothing to divide.
 *
 * A rate over zero samples is not zero percent, and showing it as such invents
 * a fact.
 *
 * @param {number | null | undefined} ratio
 * @returns {string}
 */
export function formatRate(ratio) {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return '—';
  return `${Math.round(ratio * 100)}%`;
}

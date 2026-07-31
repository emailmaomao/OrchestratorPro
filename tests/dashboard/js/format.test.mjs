/**
 * Unit tests for the dashboard's derivations.
 *
 * This is where the dashboard would most plausibly be wrong: a misread status,
 * a rate computed over nothing, a "complete" run reported as a successful one.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  aggregateMetrics,
  clampPercent,
  describeEvent,
  formatAge,
  formatClock,
  formatCost,
  formatDuration,
  formatNumber,
  formatRate,
  isActiveState,
  isTerminalState,
  shortId,
  summarizeBuild,
  summarizeRun,
  toneForBuildStatus,
  toneForEventType,
  toneForRunStatus,
  toneForTaskState,
  truncate,
} from '../../../orchestrator/dashboard/static/assets/js/format.js';

describe('formatDuration', () => {
  it('reports sub-second work in milliseconds', () => {
    assert.equal(formatDuration(0.25), '250ms');
  });

  it('keeps one decimal under ten seconds', () => {
    assert.equal(formatDuration(4.25), '4.3s');
  });

  it('drops the decimal once it stops mattering', () => {
    assert.equal(formatDuration(42.4), '42s');
  });

  it('switches to minutes', () => {
    assert.equal(formatDuration(125), '2m 5s');
  });

  it('switches to hours', () => {
    assert.equal(formatDuration(7500), '2h 5m');
  });

  it('renders nothing known as a dash', () => {
    assert.equal(formatDuration(null), '—');
    assert.equal(formatDuration(undefined), '—');
    assert.equal(formatDuration(Number.NaN), '—');
  });

  it('handles zero', () => {
    assert.equal(formatDuration(0), '0ms');
  });
});

describe('formatNumber', () => {
  it('groups thousands', () => {
    assert.equal(formatNumber(1234567), '1,234,567');
  });

  it('renders nothing known as a dash', () => {
    assert.equal(formatNumber(null), '—');
  });

  it('keeps zero as zero', () => {
    assert.equal(formatNumber(0), '0');
  });
});

describe('formatCost', () => {
  it('keeps an unpriced run distinct from a free one', () => {
    // A model that reports no cost is not a run that cost nothing.
    assert.equal(formatCost(null), 'not reported');
    assert.equal(formatCost(0), '$0.00');
  });

  it('shows more precision for small amounts', () => {
    assert.equal(formatCost(0.0042), '$0.0042');
    assert.equal(formatCost(1.5), '$1.50');
  });
});

describe('formatClock and formatAge', () => {
  it('renders the clock part of a timestamp', () => {
    assert.equal(formatClock('2026-07-26T12:34:56.789Z'), '12:34:56');
  });

  it('rejects a malformed timestamp rather than printing NaN', () => {
    assert.equal(formatClock('not a date'), '—');
    assert.equal(formatClock(null), '—');
  });

  it('renders ages at each scale', () => {
    const now = new Date('2026-07-26T12:00:00Z');
    assert.equal(formatAge('2026-07-26T11:59:50Z', now), '10s ago');
    assert.equal(formatAge('2026-07-26T11:50:00Z', now), '10m ago');
    assert.equal(formatAge('2026-07-26T09:00:00Z', now), '3h ago');
    assert.equal(formatAge('2026-07-24T12:00:00Z', now), '2d ago');
  });

  it('does not report a future timestamp as a negative age', () => {
    const now = new Date('2026-07-26T12:00:00Z');
    assert.equal(formatAge('2026-07-26T12:00:05Z', now), 'just now');
  });
});

describe('shortId', () => {
  it('keeps the prefix, which says what the thing is', () => {
    assert.equal(shortId('run_01KYEJTHTBNW4BF20B0QMTNK47'), 'run_…MTNK47');
  });

  it('leaves a short identifier alone', () => {
    assert.equal(shortId('run_1'), 'run_1');
  });

  it('handles an identifier with no prefix', () => {
    assert.equal(shortId('0123456789abcdef'), '…abcdef');
  });

  it('renders nothing as a dash', () => {
    assert.equal(shortId(null), '—');
  });
});

describe('truncate and clampPercent', () => {
  it('marks that something was removed', () => {
    assert.equal(truncate('abcdefghij', 5), 'abcd…');
  });

  it('leaves short text alone', () => {
    assert.equal(truncate('abc', 5), 'abc');
    assert.equal(truncate(null), '');
  });

  it('clamps a percentage into what a bar can draw', () => {
    assert.equal(clampPercent(-5), 0);
    assert.equal(clampPercent(140), 100);
    assert.equal(clampPercent(42.5), 42.5);
    assert.equal(clampPercent(null), 0);
  });
});

describe('tones', () => {
  it('separates a broken tool from failing code', () => {
    // FR-4.4: a UI that renders both red teaches its operator to stop reading.
    assert.equal(toneForBuildStatus('failed'), 'bad');
    assert.equal(toneForBuildStatus('errored'), 'warn');
    assert.equal(toneForBuildStatus('timed_out'), 'warn');
  });

  it('classifies task states', () => {
    assert.equal(toneForTaskState('succeeded'), 'ok');
    assert.equal(toneForTaskState('running'), 'busy');
    assert.equal(toneForTaskState('abandoned'), 'bad');
    assert.equal(toneForTaskState('pending'), 'idle');
  });

  it('classifies run statuses', () => {
    assert.equal(toneForRunStatus('finished'), 'ok');
    assert.equal(toneForRunStatus('cancelled'), 'warn');
    assert.equal(toneForRunStatus('running'), 'busy');
  });

  it('falls back to idle for anything unrecognized', () => {
    // A newer server may emit a state this build has never heard of.
    assert.equal(toneForTaskState('quantum'), 'idle');
    assert.equal(toneForBuildStatus('quantum'), 'idle');
    assert.equal(toneForRunStatus('quantum'), 'idle');
  });

  it('classifies event types by their verb', () => {
    assert.equal(toneForEventType('task.failed'), 'bad');
    assert.equal(toneForEventType('task.succeeded'), 'ok');
    assert.equal(toneForEventType('run.cancelled'), 'warn');
    assert.equal(toneForEventType('run.started'), 'busy');
    assert.equal(toneForEventType('tool.called'), 'idle');
  });
});

describe('state predicates', () => {
  it('knows which states are active', () => {
    assert.equal(isActiveState('running'), true);
    assert.equal(isActiveState('gating'), true);
    assert.equal(isActiveState('pending'), false);
  });

  it('knows which states are terminal', () => {
    assert.equal(isTerminalState('succeeded'), true);
    assert.equal(isTerminalState('abandoned'), true);
    assert.equal(isTerminalState('running'), false);
  });
});

describe('summarizeRun', () => {
  const status = (over) => ({
    active: false,
    total: 3,
    complete: true,
    healthy: true,
    summary: 's',
    ...over,
  });

  it('reports an executing run as running', () => {
    assert.equal(summarizeRun(status({ active: true })).label, 'running');
  });

  it('does not call a finished-but-failed run a success', () => {
    // `complete` means every step stopped, not that every step worked.
    const result = summarizeRun(status({ healthy: false }));
    assert.equal(result.label, 'failed');
    assert.equal(result.tone, 'bad');
  });

  it('reports a clean run as succeeded', () => {
    assert.equal(summarizeRun(status()).label, 'succeeded');
  });

  it('reports a run that stopped short as stopped', () => {
    const result = summarizeRun(status({ complete: false }));
    assert.equal(result.label, 'stopped');
    assert.equal(result.tone, 'warn');
  });

  it('reports a run with no tasks as empty', () => {
    assert.equal(summarizeRun(status({ total: 0 })).label, 'empty');
  });

  it('tolerates nothing at all', () => {
    assert.equal(summarizeRun(null).label, 'unknown');
  });
});

describe('summarizeBuild', () => {
  it('carries the status through with its tone', () => {
    assert.deepEqual(summarizeBuild({ status: 'errored', summary: 'x' }), {
      tone: 'warn',
      label: 'errored',
      detail: 'x',
    });
  });

  it('tolerates nothing at all', () => {
    assert.equal(summarizeBuild(undefined).label, 'unknown');
  });
});

describe('describeEvent', () => {
  const at = '2026-07-26T08:09:10Z';

  it('renders a run creation with its goal', () => {
    const line = describeEvent({ type: 'run.created', ts: at, payload: { goal: 'ship it' } });
    assert.equal(line.time, '08:09:10');
    assert.equal(line.kind, 'run.created');
    assert.equal(line.text, 'goal: ship it');
  });

  it('renders a failure with its code', () => {
    const line = describeEvent({
      type: 'task.failed',
      ts: at,
      payload: { attempt: 2, error_code: 'gate_failed' },
    });
    assert.equal(line.text, 'attempt 2 — gate_failed');
    assert.equal(line.tone, 'bad');
  });

  it('renders a gate verdict', () => {
    const line = describeEvent({
      type: 'gate.evaluated',
      ts: at,
      payload: { gate: 'unit', verdict: 'failed' },
    });
    assert.equal(line.text, 'unit: failed');
  });

  it('renders build events', () => {
    assert.equal(
      describeEvent({ type: 'build.unit_finished', ts: at, payload: { unit: 'core', status: 'failed' } }).text,
      'core: failed',
    );
    assert.equal(
      describeEvent({ type: 'build.started', ts: at, payload: { units: ['a', 'b'] } }).text,
      '2 unit(s) to rebuild',
    );
  });

  it('falls back to a compact payload for an unknown type', () => {
    const line = describeEvent({
      type: 'something.new',
      ts: at,
      payload: { a: 1, b: [1, 2], c: { d: 1 } },
    });
    assert.equal(line.text, 'a=1 b=[2] c={…}');
  });

  it('renders an unknown type with no payload as empty', () => {
    assert.equal(describeEvent({ type: 'x.y', ts: at, payload: {} }).text, '');
  });

  it('tolerates a malformed event', () => {
    const line = describeEvent({});
    assert.equal(line.kind, 'unknown');
    assert.equal(line.time, '—');
  });
});

describe('aggregateMetrics', () => {
  it('is all zeros and dashes for nothing', () => {
    const metrics = aggregateMetrics();
    assert.equal(metrics.runs, 0);
    assert.equal(metrics.runSuccessRate, null);
    assert.equal(metrics.cacheHitRate, null);
  });

  it('counts runs and how many are executing', () => {
    const metrics = aggregateMetrics({
      runs: [{ active: true }, { active: false }, { active: false }],
    });
    assert.equal(metrics.runs, 3);
    assert.equal(metrics.activeRuns, 1);
  });

  it('computes a success rate over finished runs only', () => {
    const metrics = aggregateMetrics({
      statuses: [
        { active: false, total: 2, complete: true, healthy: true, failed: 0 },
        { active: false, total: 2, complete: true, healthy: false, failed: 1 },
        { active: true, total: 2, complete: false, healthy: true, failed: 0 },
      ],
    });
    assert.equal(metrics.finishedRuns, 2);
    assert.equal(metrics.runSuccessRate, 0.5);
    assert.equal(metrics.taskFailures, 1);
    assert.equal(metrics.tasks, 6);
  });

  it('ignores an empty run when computing the rate', () => {
    const metrics = aggregateMetrics({
      statuses: [{ active: false, total: 0, complete: false, healthy: true }],
    });
    assert.equal(metrics.finishedRuns, 0);
    assert.equal(metrics.runSuccessRate, null);
  });

  it('computes a cache hit rate from build reports', () => {
    const metrics = aggregateMetrics({
      builds: [
        { status: 'succeeded', duration_s: 2, rebuilt: ['a'], cached: ['b', 'c'] },
        { status: 'running', duration_s: 0, rebuilt: [], cached: [] },
      ],
    });
    assert.equal(metrics.builds, 1);
    assert.equal(metrics.buildSeconds, 2);
    assert.equal(metrics.unitsRebuilt, 1);
    assert.equal(metrics.unitsCached, 2);
    assert.equal(metrics.cacheHitRate, 2 / 3);
  });
});

describe('formatRate', () => {
  it('renders a ratio as a percentage', () => {
    assert.equal(formatRate(0.5), '50%');
    assert.equal(formatRate(1), '100%');
  });

  it('does not invent a rate over no samples', () => {
    // Zero of zero is not zero percent.
    assert.equal(formatRate(null), '—');
    assert.equal(formatRate(undefined), '—');
  });
});

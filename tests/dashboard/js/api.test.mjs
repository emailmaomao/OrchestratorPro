/**
 * Unit tests for the dashboard's API client.
 *
 * Run by `tests/dashboard/test_frontend_units.py` through `node --test`, so
 * they are part of the ordinary suite rather than a thing somebody has to
 * remember to run. `fetch` and `EventSource` are injected, so nothing here
 * opens a socket.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { Api, ApiError, apiBase, parseFrame, queryString } from '../../../orchestrator/dashboard/static/assets/js/api.js';

/** A fetch stub that records calls and replies from a script. */
function stubFetch(replies) {
  const calls = [];
  const queue = Array.isArray(replies) ? [...replies] : [replies];
  const impl = async (url, options) => {
    calls.push({ url, options });
    const reply = queue.length > 1 ? queue.shift() : queue[0];
    if (reply instanceof Error) throw reply;
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      async json() {
        if (reply.body === undefined) throw new SyntaxError('no body');
        return reply.body;
      },
    };
  };
  impl.calls = calls;
  return impl;
}

const okReply = (body = {}) => ({ status: 200, body });

describe('queryString', () => {
  it('renders nothing for no parameters', () => {
    assert.equal(queryString(), '');
    assert.equal(queryString({}), '');
  });

  it('renders parameters with a leading question mark', () => {
    assert.equal(queryString({ limit: 5 }), '?limit=5');
  });

  it('drops empty values so a blank filter is not sent', () => {
    assert.equal(queryString({ a: undefined, b: null, c: '', d: 1 }), '?d=1');
  });

  it('keeps a false boolean, which is a real choice', () => {
    assert.equal(queryString({ replay: false }), '?replay=false');
  });

  it('escapes values', () => {
    assert.equal(queryString({ q: 'a b&c' }), '?q=a+b%26c');
  });
});

describe('parseFrame', () => {
  it('decodes a frame', () => {
    assert.deepEqual(parseFrame({ data: '{"type":"run.created"}' }), {
      type: 'run.created',
    });
  });

  it('drops a malformed frame rather than throwing', () => {
    // A stream is long-lived; one bad message must not end the operator's view.
    assert.equal(parseFrame({ data: 'not json' }), null);
  });

  it('drops an empty frame', () => {
    assert.equal(parseFrame({}), null);
    assert.equal(parseFrame(null), null);
  });
});

describe('apiBase', () => {
  it('is empty when nothing is declared', () => {
    assert.equal(apiBase({ querySelector: () => null }), '');
  });

  it('reads a declared base', () => {
    assert.equal(apiBase({ querySelector: () => ({ content: 'http://host:80/' }) }), 'http://host:80');
  });

  it('tolerates no document at all', () => {
    assert.equal(apiBase(undefined), '');
  });
});

describe('Api.request', () => {
  it('builds the URL from the base and the path', async () => {
    const fetchImpl = stubFetch(okReply({ status: 'ok' }));
    const api = new Api({ base: 'http://server', fetch: fetchImpl });

    await api.health();

    assert.equal(fetchImpl.calls[0].url, 'http://server/health');
    assert.equal(fetchImpl.calls[0].options.method, 'GET');
  });

  it('strips a trailing slash from the base', async () => {
    const fetchImpl = stubFetch(okReply());
    await new Api({ base: 'http://server/', fetch: fetchImpl }).health();

    assert.equal(fetchImpl.calls[0].url, 'http://server/health');
  });

  it('returns the decoded body', async () => {
    const api = new Api({ base: '', fetch: stubFetch(okReply({ runs: 3 })) });
    assert.deepEqual(await api.health(), { runs: 3 });
  });

  it('returns null for a 204', async () => {
    const api = new Api({ base: '', fetch: stubFetch({ status: 204 }) });
    assert.equal(await api.request('/whatever', { method: 'DELETE' }), null);
  });

  it('sends a JSON body with the right content type', async () => {
    const fetchImpl = stubFetch(okReply());
    const api = new Api({ base: '', fetch: fetchImpl });

    await api.startBuild({ path: '/p' });

    const { options } = fetchImpl.calls[0];
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['content-type'], 'application/json');
    assert.equal(options.body, '{"path":"/p"}');
  });

  it('sends no content type when there is no body', async () => {
    const fetchImpl = stubFetch(okReply());
    await new Api({ base: '', fetch: fetchImpl }).health();

    assert.equal(fetchImpl.calls[0].options.headers['content-type'], undefined);
    assert.equal(fetchImpl.calls[0].options.body, undefined);
  });
});

describe('Api errors', () => {
  it('unwraps the API error envelope', async () => {
    const api = new Api({
      base: '',
      fetch: stubFetch({
        status: 404,
        body: {
          error: {
            code: 'not_found',
            message: 'no run with id x',
            retryable: false,
            detail: { run_id: 'x' },
          },
        },
      }),
    });

    await assert.rejects(
      () => api.run('x'),
      (error) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.code, 'not_found');
        assert.equal(error.status, 404);
        assert.equal(error.detail.run_id, 'x');
        assert.equal(error.isMissing, true);
        return true;
      },
    );
  });

  it('survives an error response that is not JSON', async () => {
    const api = new Api({ base: '', fetch: stubFetch({ status: 500 }) });

    await assert.rejects(
      () => api.health(),
      (error) => {
        assert.equal(error.code, 'http_500');
        assert.equal(error.retryable, true);
        return true;
      },
    );
  });

  it('reports an unreachable server distinctly from a refusal', async () => {
    // An operator staring at a blank page needs to know which one happened.
    const api = new Api({ base: '', fetch: stubFetch(new TypeError('network down')) });

    await assert.rejects(
      () => api.health(),
      (error) => {
        assert.equal(error.code, 'unreachable');
        assert.equal(error.retryable, true);
        assert.equal(error.status, 0);
        return true;
      },
    );
  });

  it('lets an abort through untouched', async () => {
    const aborted = new Error('aborted');
    aborted.name = 'AbortError';
    const api = new Api({ base: '', fetch: stubFetch(aborted) });

    await assert.rejects(() => api.health(), { name: 'AbortError' });
  });

  it('marks a 5xx retryable and a 4xx not', async () => {
    const server = new Api({ base: '', fetch: stubFetch({ status: 503, body: {} }) });
    const client = new Api({ base: '', fetch: stubFetch({ status: 400, body: {} }) });

    await assert.rejects(() => server.health(), (e) => e.retryable === true);
    await assert.rejects(() => client.health(), (e) => e.retryable === false);
  });
});

describe('Api endpoints', () => {
  const cases = [
    ['health', [], '/health'],
    ['config', [], '/config'],
    ['runs', [{ limit: 10 }], '/runs?limit=10'],
    ['run', ['run_1'], '/runs/run_1'],
    ['runStatus', ['run_1'], '/runs/run_1/status'],
    ['cancelRun', ['run_1'], '/runs/run_1/cancel'],
    ['tasks', ['run_1'], '/runs/run_1/tasks'],
    ['tasks', ['run_1', 'pending'], '/runs/run_1/tasks?state=pending'],
    ['workflows', [], '/workflows'],
    ['workflow', ['ship'], '/workflows/ship'],
    ['startWorkflow', ['ship'], '/workflows/ship/runs'],
    ['agentRoles', [], '/agents/roles'],
    ['agentTools', [], '/agents/tools'],
    ['renderPrompt', [{ title: 't', prompt: 'p' }], '/agents/prompt'],
    ['builds', [], '/builds'],
    ['build', ['build_1'], '/builds/build_1'],
    ['planBuild', [{ path: '/p' }], '/builds/plan'],
    ['startBuild', [{ path: '/p' }], '/builds'],
    ['clearBuildCache', [], '/builds/cache'],
  ];

  for (const [method, args, expected] of cases) {
    it(`${method} calls ${expected}`, async () => {
      const fetchImpl = stubFetch(okReply());
      const api = new Api({ base: '', fetch: fetchImpl });

      await api[method](...args);

      assert.equal(fetchImpl.calls[0].url, expected);
    });
  }

  it('escapes an identifier that would otherwise change the path', async () => {
    const fetchImpl = stubFetch(okReply());
    await new Api({ base: '', fetch: fetchImpl }).run('../health');

    assert.equal(fetchImpl.calls[0].url, '/runs/..%2Fhealth');
  });

  it('resume names the workflow it is resuming as', async () => {
    const fetchImpl = stubFetch(okReply());
    await new Api({ base: '', fetch: fetchImpl }).resumeRun('run_1', 'ship', true);

    assert.equal(
      fetchImpl.calls[0].url,
      '/runs/run_1/resume?workflow=ship&retry_failed=true',
    );
  });
});

describe('Api.openEvents', () => {
  /** A minimal EventSource stand-in. */
  class FakeSource {
    constructor(url) {
      this.url = url;
      this.closed = false;
      FakeSource.last = this;
    }
    close() {
      this.closed = true;
    }
  }

  it('streams every run when no run is named', () => {
    const api = new Api({ base: '', eventSource: FakeSource });
    api.openEvents({ onEvent() {} });

    assert.equal(FakeSource.last.url, '/events');
  });

  it('scopes to one run and asks for the replay', () => {
    const api = new Api({ base: '', eventSource: FakeSource });
    api.openEvents({ runId: 'run_1', onEvent() {} });

    assert.equal(FakeSource.last.url, '/runs/run_1/events?replay=true');
  });

  it('can decline the replay', () => {
    const api = new Api({ base: '', eventSource: FakeSource });
    api.openEvents({ runId: 'run_1', replay: false, onEvent() {} });

    assert.equal(FakeSource.last.url, '/runs/run_1/events?replay=false');
  });

  it('delivers decoded events', () => {
    const seen = [];
    const api = new Api({ base: '', eventSource: FakeSource });
    api.openEvents({ onEvent: (event) => seen.push(event) });

    FakeSource.last.onmessage({ data: '{"type":"run.started"}' });

    assert.deepEqual(seen, [{ type: 'run.started' }]);
  });

  it('does not deliver a malformed frame', () => {
    const seen = [];
    const api = new Api({ base: '', eventSource: FakeSource });
    api.openEvents({ onEvent: (event) => seen.push(event) });

    FakeSource.last.onmessage({ data: '{{{' });

    assert.deepEqual(seen, []);
  });

  it('closes the underlying source', () => {
    const api = new Api({ base: '', eventSource: FakeSource });
    const handle = api.openEvents({ onEvent() {} });

    handle.close();

    assert.equal(FakeSource.last.closed, true);
  });

  it('reports a browser with no EventSource rather than failing obscurely', () => {
    const api = new Api({ base: '', eventSource: undefined });
    api._EventSource = undefined;

    assert.throws(() => api.openEvents({ onEvent() {} }), { code: 'unsupported' });
  });
});

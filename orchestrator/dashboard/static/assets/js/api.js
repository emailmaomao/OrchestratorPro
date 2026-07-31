/**
 * The only module that talks to the network.
 *
 * Every other module receives data and returns markup. That is not a style
 * preference: the dashboard must not grow a second idea of what a run is, and
 * the cheapest way to guarantee that is to have exactly one place where facts
 * enter the program. A test asserts that no other module calls `fetch` or opens
 * an `EventSource`.
 *
 * Errors arrive from the API in one envelope — `{error: {code, message,
 * retryable, detail}}` — so they are unwrapped here into an `ApiError` carrying
 * the same stable `code` the server used. A page never parses prose.
 */

/** An error returned by the API, or a failure to reach it. */
export class ApiError extends Error {
  /**
   * @param {string} code Stable machine-readable code.
   * @param {string} message Human-readable description.
   * @param {object} [options]
   * @param {number} [options.status] HTTP status, when there was one.
   * @param {boolean} [options.retryable] Whether repeating could succeed.
   * @param {object} [options.detail] Structured context.
   */
  constructor(code, message, { status = 0, retryable = false, detail = {} } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.detail = detail;
  }

  /** Whether this is the API saying "not here" rather than "not working". */
  get isMissing() {
    return this.status === 404;
  }
}

/**
 * Resolve the API's base URL.
 *
 * The dashboard is mounted on the same application as the API, so the base is
 * the origin. `<base href="/ui/">` governs asset paths, not these — API paths
 * are absolute on purpose, so a client-side route three levels deep still hits
 * `/runs` rather than `/ui/runs/runs`.
 *
 * @param {Document} [doc]
 * @returns {string} The base, without a trailing slash.
 */
export function apiBase(doc = globalThis.document) {
  const declared = doc?.querySelector?.('meta[name="api-base"]')?.content;
  return (declared ?? '').replace(/\/$/, '');
}

/** A client for the OrchestratorPro API. */
export class Api {
  /**
   * @param {object} [options]
   * @param {string} [options.base] Base URL. Defaults to the same origin.
   * @param {typeof fetch} [options.fetch] Injected for tests.
   * @param {typeof EventSource} [options.eventSource] Injected for tests.
   */
  constructor({ base, fetch: fetchImpl, eventSource } = {}) {
    this.base = (base ?? apiBase()).replace(/\/$/, '');
    this._fetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
    this._EventSource = eventSource ?? globalThis.EventSource;
  }

  /**
   * Perform one request and decode it.
   *
   * @param {string} path Path below the base, starting with `/`.
   * @param {object} [options]
   * @param {string} [options.method]
   * @param {object} [options.body] JSON body.
   * @param {object} [options.query] Query parameters; `undefined` values drop.
   * @param {AbortSignal} [options.signal]
   * @returns {Promise<any>} The decoded body, or `null` for `204`.
   * @throws {ApiError} On any non-2xx response, or if the server is unreachable.
   */
  async request(path, { method = 'GET', body, query, signal } = {}) {
    const url = this.base + path + queryString(query);
    let response;
    try {
      response = await this._fetch(url, {
        method,
        signal,
        headers: body === undefined ? { accept: 'application/json' } : {
          accept: 'application/json',
          'content-type': 'application/json',
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (cause) {
      if (cause?.name === 'AbortError') throw cause;
      // The server being unreachable is a different failure from the server
      // saying no, and an operator staring at a blank page needs to know which.
      throw new ApiError('unreachable', `could not reach the API at ${url}`, {
        retryable: true,
        detail: { url, cause: String(cause?.message ?? cause) },
      });
    }

    if (response.status === 204) return null;

    const payload = await readJson(response);
    if (!response.ok) {
      const error = payload?.error ?? {};
      throw new ApiError(
        error.code ?? `http_${response.status}`,
        error.message ?? `${method} ${path} failed with ${response.status}`,
        {
          status: response.status,
          retryable: error.retryable ?? response.status >= 500,
          detail: error.detail ?? {},
        },
      );
    }
    return payload;
  }

  // --- system ------------------------------------------------------------

  /** @returns {Promise<any>} Liveness and what the server can do. */
  health() {
    return this.request('/health');
  }

  /** @returns {Promise<any>} The effective configuration. */
  config() {
    return this.request('/config');
  }

  /** @returns {Promise<any>} The generated OpenAPI document. */
  openapi() {
    return this.request('/openapi.json');
  }

  // --- runs --------------------------------------------------------------

  /**
   * @param {object} [query]
   * @param {number} [query.limit]
   * @param {boolean} [query.active]
   * @returns {Promise<any[]>}
   */
  runs(query = {}) {
    return this.request('/runs', { query });
  }

  /** @param {string} id @returns {Promise<any>} */
  run(id) {
    return this.request(`/runs/${encodeURIComponent(id)}`);
  }

  /** @param {string} id @returns {Promise<any>} */
  runStatus(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/status`);
  }

  /** @param {string} id @returns {Promise<any>} */
  cancelRun(id) {
    return this.request(`/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  }

  /**
   * @param {string} id
   * @param {string} workflow The workflow to resume it as.
   * @param {boolean} [retryFailed]
   * @returns {Promise<any>}
   */
  resumeRun(id, workflow, retryFailed = false) {
    return this.request(`/runs/${encodeURIComponent(id)}/resume`, {
      method: 'POST',
      query: { workflow, retry_failed: retryFailed },
    });
  }

  /** @param {string} id @param {string} [state] @returns {Promise<any[]>} */
  tasks(id, state) {
    return this.request(`/runs/${encodeURIComponent(id)}/tasks`, { query: { state } });
  }

  /**
   * Read a run's recorded events.
   *
   * For history. Watching a run happen is what {@link Api#openEvents} is for.
   *
   * @param {string} id
   * @param {object} [query]
   * @param {number} [query.limit]
   * @param {string} [query.after] Exclusive lower bound, for paging.
   * @returns {Promise<any[]>}
   */
  log(id, query = {}) {
    return this.request(`/runs/${encodeURIComponent(id)}/log`, { query });
  }

  // --- workflows ---------------------------------------------------------

  /** @returns {Promise<any[]>} */
  workflows() {
    return this.request('/workflows');
  }

  /** @param {string} name @returns {Promise<any>} */
  workflow(name) {
    return this.request(`/workflows/${encodeURIComponent(name)}`);
  }

  /** @param {string} name @param {object} [body] @returns {Promise<any>} */
  startWorkflow(name, body = {}) {
    return this.request(`/workflows/${encodeURIComponent(name)}/runs`, {
      method: 'POST',
      body,
    });
  }

  // --- agents ------------------------------------------------------------

  /** @returns {Promise<any[]>} */
  agentRoles() {
    return this.request('/agents/roles');
  }

  /** @returns {Promise<any[]>} */
  agentTools() {
    return this.request('/agents/tools');
  }

  /** @param {object} body @returns {Promise<any>} */
  renderPrompt(body) {
    return this.request('/agents/prompt', { method: 'POST', body });
  }

  // --- builds ------------------------------------------------------------

  /** @returns {Promise<any[]>} */
  builds() {
    return this.request('/builds');
  }

  /** @param {string} id @returns {Promise<any>} */
  build(id) {
    return this.request(`/builds/${encodeURIComponent(id)}`);
  }

  /** @param {object} body @returns {Promise<any>} */
  planBuild(body) {
    return this.request('/builds/plan', { method: 'POST', body });
  }

  /** @param {object} body @returns {Promise<any>} */
  startBuild(body) {
    return this.request('/builds', { method: 'POST', body });
  }

  /** @returns {Promise<any>} */
  clearBuildCache() {
    return this.request('/builds/cache', { method: 'DELETE' });
  }

  // --- events ------------------------------------------------------------

  /**
   * Open a Server-Sent Events stream.
   *
   * SSE rather than the WebSocket endpoint: the dashboard only ever reads, and
   * SSE reconnects by itself. The socket is there for clients that need to talk
   * back, which this one never does.
   *
   * @param {object} [options]
   * @param {string} [options.runId] Scope to one run. Omitted, every run.
   * @param {boolean} [options.replay] Send the log so far first.
   * @param {(event: object) => void} options.onEvent
   * @param {(error: Event) => void} [options.onError]
   * @returns {{close: () => void}} A handle; call `close` when done.
   */
  openEvents({ runId, replay = true, onEvent, onError } = {}) {
    if (!this._EventSource) {
      throw new ApiError('unsupported', 'this browser has no EventSource');
    }
    const path = runId
      ? `/runs/${encodeURIComponent(runId)}/events${queryString({ replay })}`
      : '/events';
    const source = new this._EventSource(this.base + path);

    source.onmessage = (message) => {
      const parsed = parseFrame(message);
      if (parsed) onEvent(parsed);
    };
    if (onError) source.onerror = onError;

    return {
      close() {
        source.close();
      },
      source,
    };
  }
}

/**
 * Decode one SSE frame's payload.
 *
 * A malformed frame is dropped rather than thrown: a stream is a long-lived
 * thing, and one unparseable message must not end the operator's view of a run.
 *
 * @param {{data?: string}} message
 * @returns {object | null}
 */
export function parseFrame(message) {
  if (!message?.data) return null;
  try {
    return JSON.parse(message.data);
  } catch {
    return null;
  }
}

/**
 * Render a query string, dropping empty values.
 *
 * @param {object} [query]
 * @returns {string} Including the leading `?`, or empty.
 */
export function queryString(query) {
  if (!query) return '';
  const parts = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    parts.set(key, typeof value === 'boolean' ? String(value) : value);
  }
  const rendered = parts.toString();
  return rendered ? `?${rendered}` : '';
}

/**
 * Read a response body as JSON, tolerating one that is not.
 *
 * @param {Response} response
 * @returns {Promise<any>}
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** A ready-made client against the current origin. */
export const api = new Api();

/**
 * The shell: routing, page lifecycle, and the shared live event stream.
 *
 * Pages are plain objects with a `render(context)` that returns a node. They
 * never reach for globals: the API client, the route parameters, and a cleanup
 * registry all arrive in the context, so a page can be exercised in isolation
 * and cannot leak a timer into the next one.
 *
 * There is exactly one event stream for the whole application. Opening one per
 * page would mean the number of connections grew with how much the operator
 * clicked around, and the server would carry a subscription for every view
 * anybody had ever visited.
 */

import { api, ApiError } from './api.js';
import { describeEvent } from './format.js';
import { el, errorState, loading, replace } from './ui.js';

import { agentsPage } from './pages/agents.js';
import { buildsPage } from './pages/builds.js';
import { eventsPage } from './pages/events.js';
import { runsPage, runDetailPage } from './pages/runs.js';
import { configPage, metricsPage, statusPage } from './pages/system.js';
import { workflowsPage, workflowDetailPage } from './pages/workflows.js';

/**
 * The route table, in match order.
 *
 * Patterns use `:name` for a segment that becomes a parameter. First match
 * wins, so specific routes precede general ones.
 */
const ROUTES = [
  { pattern: 'runs/:id', page: runDetailPage, section: 'runs' },
  { pattern: 'runs', page: runsPage, section: 'runs' },
  { pattern: 'workflows/:name', page: workflowDetailPage, section: 'workflows' },
  { pattern: 'workflows', page: workflowsPage, section: 'workflows' },
  { pattern: 'agents', page: agentsPage, section: 'agents' },
  { pattern: 'builds', page: buildsPage, section: 'builds' },
  { pattern: 'events', page: eventsPage, section: 'events' },
  { pattern: 'metrics', page: metricsPage, section: 'metrics' },
  { pattern: 'config', page: configPage, section: 'config' },
  { pattern: 'status', page: statusPage, section: 'status' },
];

/** Where an empty path goes. */
const DEFAULT_ROUTE = 'runs';

/**
 * Match a path against the route table.
 *
 * @param {string} path Path below the dashboard's base, without a leading slash.
 * @returns {{route: object, params: object} | null}
 */
export function matchRoute(path) {
  const segments = path.split('/').filter(Boolean);
  for (const route of ROUTES) {
    const pattern = route.pattern.split('/');
    if (pattern.length !== segments.length) continue;

    const params = {};
    let matched = true;
    for (const [index, part] of pattern.entries()) {
      if (part.startsWith(':')) {
        params[part.slice(1)] = decodeURIComponent(segments[index]);
      } else if (part !== segments[index]) {
        matched = false;
        break;
      }
    }
    if (matched) return { route, params };
  }
  return null;
}

/**
 * A broadcaster for the live event stream.
 *
 * One connection, many listeners. Listeners are held in a set rather than an
 * array so that unsubscribing during a delivery cannot skip the next listener.
 */
export class EventHub {
  /** @param {import('./api.js').Api} client */
  constructor(client) {
    this._api = client;
    this._listeners = new Set();
    this._handle = null;
    this._recent = [];
    this._limit = 500;
    this.connected = false;
  }

  /** Events seen since the page loaded, oldest first. */
  get recent() {
    return [...this._recent];
  }

  /**
   * Register a listener.
   *
   * @param {(event: object) => void} listener
   * @returns {() => void} Unsubscribe.
   */
  subscribe(listener) {
    this._listeners.add(listener);
    this._open();
    return () => {
      this._listeners.delete(listener);
      if (!this._listeners.size) this.close();
    };
  }

  /** Open the stream if it is not already open. */
  _open() {
    if (this._handle) return;
    try {
      this._handle = this._api.openEvents({
        onEvent: (event) => this._deliver(event),
        onError: () => {
          // EventSource reconnects by itself; the pill reflects the gap rather
          // than the page throwing away everything it had.
          this.connected = false;
          this._deliver({ type: 'stream.interrupted', ts: new Date().toISOString(), payload: {} });
        },
      });
      this.connected = true;
    } catch (error) {
      this.connected = false;
      this._deliver({
        type: 'stream.unavailable',
        ts: new Date().toISOString(),
        payload: { message: error?.message ?? String(error) },
      });
    }
  }

  /**
   * Fan one event out.
   *
   * A listener that throws must not stop the others: one broken view should
   * not take the rest of the dashboard down with it.
   *
   * @param {object} event
   */
  _deliver(event) {
    if (event?.id || event?.type?.startsWith('stream.')) {
      this._recent.push(event);
      if (this._recent.length > this._limit) this._recent.shift();
    }
    if (event?.id) this.connected = true;
    for (const listener of [...this._listeners]) {
      try {
        listener(event);
      } catch (error) {
        console.error('event listener failed', error);
      }
    }
  }

  /** Close the stream and forget the listeners. */
  close() {
    this._handle?.close();
    this._handle = null;
    this.connected = false;
  }
}

/** Drives routing and page rendering. */
export class App {
  /**
   * @param {object} [options]
   * @param {import('./api.js').Api} [options.api]
   * @param {Document} [options.doc]
   */
  constructor({ api: client = api, doc = document } = {}) {
    this.api = client;
    this.doc = doc;
    this.hub = new EventHub(client);
    this.view = doc.getElementById('view');
    this.base = new URL(doc.baseURI).pathname;
    this._cleanups = [];
    this._token = 0;
  }

  /** Wire up the shell and render the first page. */
  start() {
    this.doc.addEventListener('click', (event) => this._onClick(event));
    globalThis.addEventListener?.('popstate', () => this.render());
    this._watchHealth();
    this.render();
  }

  /**
   * Navigate to a path below the base.
   *
   * @param {string} path
   */
  navigate(path) {
    const target = this.base.replace(/\/$/, '') + '/' + path.replace(/^\//, '');
    globalThis.history?.pushState({}, '', target);
    this.render();
  }

  /** The current path below the base. */
  get path() {
    const full = globalThis.location?.pathname ?? this.base;
    const trimmed = full.startsWith(this.base) ? full.slice(this.base.length) : full;
    return trimmed.replace(/^\//, '') || DEFAULT_ROUTE;
  }

  /**
   * Intercept in-app links so navigation does not reload the document.
   *
   * @param {MouseEvent} event
   */
  _onClick(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey) {
      return;
    }
    const anchor = event.target?.closest?.('a');
    if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;

    const href = anchor.getAttribute('href') ?? '';
    if (!href || href.startsWith('http') || href.startsWith('#') || href.startsWith('mailto:')) {
      return;
    }
    event.preventDefault();
    this.navigate(href);
  }

  /** Render the page matching the current path. */
  async render() {
    const token = ++this._token;
    this._runCleanups();

    const matched = matchRoute(this.path);
    this._markNav(matched?.route?.section);

    if (!matched) {
      replace(this.view, notFound(this.path, (path) => this.navigate(path)));
      this.view.removeAttribute('aria-busy');
      return;
    }

    replace(this.view, loading());
    this.view.setAttribute('aria-busy', 'true');

    const context = {
      api: this.api,
      hub: this.hub,
      params: matched.params,
      navigate: (path) => this.navigate(path),
      onCleanup: (fn) => this._cleanups.push(fn),
      refresh: () => this.render(),
    };

    try {
      const node = await matched.route.page.render(context);
      // A navigation that happened while this page was loading wins: rendering
      // a stale result over the new page is worse than the wait.
      if (token !== this._token) return;
      replace(this.view, node);
      this.doc.title = `${matched.route.page.title} — OrchestratorPro`;
    } catch (error) {
      if (token !== this._token) return;
      replace(this.view, errorState(asApiError(error), { onRetry: () => this.render() }));
    } finally {
      if (token === this._token) this.view.removeAttribute('aria-busy');
    }
  }

  /**
   * Mark the current section in the navigation.
   *
   * @param {string | undefined} section
   */
  _markNav(section) {
    for (const anchor of this.doc.querySelectorAll('.nav a')) {
      if (anchor.dataset.route === section) {
        anchor.setAttribute('aria-current', 'page');
      } else {
        anchor.removeAttribute('aria-current');
      }
    }
  }

  /** Poll `/health` so the pill reflects the server rather than a guess. */
  _watchHealth() {
    const pill = this.doc.getElementById('health-pill');
    if (!pill) return;

    const paint = (state, text) => {
      pill.querySelector('.dot')?.setAttribute('data-state', state);
      const label = pill.querySelector('.health-text');
      if (label) label.textContent = text;
    };

    const check = async () => {
      try {
        const health = await this.api.health();
        paint(
          health.status === 'ok' ? 'ok' : 'degraded',
          health.execution_available
            ? `${health.active_runs} active · ${health.runs} runs`
            : 'read-only (no execution backend)',
        );
      } catch (error) {
        paint('down', asApiError(error).code === 'unreachable' ? 'unreachable' : 'error');
      }
    };

    check();
    const timer = setInterval(check, 5000);
    globalThis.addEventListener?.('beforeunload', () => clearInterval(timer));
  }

  /** Run and clear the cleanups registered by the outgoing page. */
  _runCleanups() {
    for (const cleanup of this._cleanups.splice(0)) {
      try {
        cleanup();
      } catch (error) {
        console.error('page cleanup failed', error);
      }
    }
  }
}

/**
 * Render a 404 for an unmatched client-side route.
 *
 * @param {string} path
 * @param {(path: string) => void} navigate
 * @returns {HTMLElement}
 */
function notFound(path, navigate) {
  return el('div', { class: 'empty' }, [
    el('p', { text: `No dashboard page at "${path}".` }),
    el('button', { text: 'Go to runs', onClick: () => navigate(DEFAULT_ROUTE) }),
  ]);
}

/**
 * Coerce anything thrown into something with a `code`.
 *
 * @param {unknown} error
 * @returns {ApiError}
 */
export function asApiError(error) {
  if (error instanceof ApiError) return error;
  return new ApiError('internal', error?.message ?? String(error));
}

/**
 * Render recent events as log lines.
 *
 * Exported so the events page and a run's log agree on the format without one
 * importing the other.
 *
 * @param {object[]} events
 * @returns {Array<object>}
 */
export function toLogLines(events) {
  return events.map(describeEvent);
}

// Start, unless something imported this module for its exports.
if (globalThis.document?.getElementById?.('view')) {
  new App().start();
}

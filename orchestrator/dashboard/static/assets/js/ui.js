/**
 * DOM construction helpers.
 *
 * Everything is built with `createElement` and `textContent`; there is no
 * `innerHTML` anywhere in the dashboard. Run goals, task prompts, build
 * diagnostics, and event payloads are all written by agents or read out of a
 * repository, and a UI that interpolates those into markup is a UI that
 * executes whatever the model felt like emitting. A test enforces the ban.
 */

import { clampPercent, formatDuration, truncate } from './format.js';

/**
 * Build an element.
 *
 * @param {string} tag
 * @param {object} [attrs] Attributes. `class`, `text`, `html` (rejected),
 *   `dataset`, and `on*` handlers are treated specially.
 * @param {Array<Node | string | null | undefined> | Node | string} [children]
 * @returns {HTMLElement}
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'class') {
      node.className = value;
    } else if (key === 'dataset') {
      for (const [name, entry] of Object.entries(value)) {
        if (entry !== undefined && entry !== null) node.dataset[name] = String(entry);
      }
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, String(value));
    }
  }
  append(node, children);
  return node;
}

/**
 * Append children, flattening arrays and skipping blanks.
 *
 * @param {Node} parent
 * @param {any} children
 * @returns {Node} The parent.
 */
export function append(parent, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) {
      append(parent, child);
    } else {
      parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return parent;
}

/**
 * Replace an element's contents.
 *
 * @param {HTMLElement} parent
 * @param {any} children
 * @returns {HTMLElement}
 */
export function replace(parent, children) {
  parent.replaceChildren();
  append(parent, children);
  return parent;
}

/**
 * A page heading with an optional lede and actions.
 *
 * @param {string} title
 * @param {object} [options]
 * @param {string} [options.lede]
 * @param {Node[]} [options.actions]
 * @returns {DocumentFragment}
 */
export function pageHeader(title, { lede, actions = [] } = {}) {
  const fragment = document.createDocumentFragment();
  fragment.append(
    el('div', { class: 'page-head' }, [
      el('h1', { text: title }),
      actions.length ? el('div', { class: 'toolbar' }, actions) : null,
    ]),
  );
  if (lede) fragment.append(el('p', { class: 'lede', text: lede }));
  return fragment;
}

/**
 * A coloured status pill.
 *
 * @param {string} label
 * @param {string} [tone]
 * @returns {HTMLElement}
 */
export function badge(label, tone = 'idle') {
  return el('span', { class: 'badge', dataset: { tone }, text: label });
}

/**
 * A progress bar.
 *
 * @param {number} percent
 * @param {string} [tone]
 * @returns {HTMLElement}
 */
export function bar(percent, tone = 'ok') {
  const value = clampPercent(percent);
  return el(
    'div',
    {
      class: 'bar',
      dataset: { tone },
      role: 'progressbar',
      'aria-valuenow': Math.round(value),
      'aria-valuemin': 0,
      'aria-valuemax': 100,
    },
    el('span', { style: `width:${value}%` }),
  );
}

/**
 * A table, or an empty state when there are no rows.
 *
 * @param {object} spec
 * @param {Array<string | {label: string, class?: string}>} spec.columns
 * @param {any[]} spec.rows
 * @param {(row: any, index: number) => Array<Node | string | null>} spec.cell
 * @param {string} [spec.empty] What to say when there is nothing.
 * @returns {HTMLElement}
 */
export function table({ columns, rows, cell, empty = 'Nothing here yet.' }) {
  if (!rows?.length) return emptyState(empty);

  const head = el(
    'tr',
    {},
    columns.map((column) =>
      typeof column === 'string'
        ? el('th', { text: column })
        : el('th', { text: column.label, class: column.class ?? '' }),
    ),
  );

  const body = rows.map((row, index) =>
    el(
      'tr',
      {},
      cell(row, index).map((value) =>
        value instanceof Node && value.tagName === 'TD' ? value : el('td', {}, value),
      ),
    ),
  );

  return el('div', { class: 'table-wrap' }, [
    el('table', {}, [el('thead', {}, head), el('tbody', {}, body)]),
  ]);
}

/**
 * A right-aligned numeric cell.
 *
 * @param {any} value
 * @returns {HTMLElement}
 */
export function num(value) {
  return el('td', { class: 'num' }, value);
}

/**
 * A definition list of facts.
 *
 * @param {Array<[string, any]>} entries
 * @returns {HTMLElement}
 */
export function facts(entries) {
  const list = el('dl', { class: 'facts' });
  for (const [term, value] of entries) {
    if (value === undefined) continue;
    list.append(el('dt', { text: term }), el('dd', {}, value ?? '—'));
  }
  return list;
}

/**
 * A statistic tile.
 *
 * @param {string} label
 * @param {any} value
 * @param {string} [note]
 * @returns {HTMLElement}
 */
export function stat(label, value, note) {
  return el('div', { class: 'stat' }, [
    el('div', { class: 'stat-label', text: label }),
    el('div', { class: 'stat-value' }, value),
    note ? el('div', { class: 'stat-note', text: note }) : null,
  ]);
}

/**
 * An empty state.
 *
 * @param {string} message
 * @returns {HTMLElement}
 */
export function emptyState(message) {
  return el('div', { class: 'empty', text: message });
}

/**
 * A placeholder shown while a page is loading.
 *
 * @param {string} [message]
 * @returns {HTMLElement}
 */
export function loading(message = 'Loading…') {
  return el('div', { class: 'skeleton', text: message });
}

/**
 * Render an error for a human, keeping the machine-readable code visible.
 *
 * @param {Error & {code?: string, detail?: object, retryable?: boolean}} error
 * @param {object} [options]
 * @param {() => void} [options.onRetry]
 * @returns {HTMLElement}
 */
export function errorState(error, { onRetry } = {}) {
  const code = error?.code ?? 'error';
  return el('div', { class: 'empty error' }, [
    el('p', { text: error?.message ?? String(error) }),
    el('p', { class: 'mono', text: `code: ${code}` }),
    onRetry && error?.retryable !== false
      ? el('button', { text: 'Try again', onClick: onRetry })
      : null,
  ]);
}

/**
 * A link that the router will intercept.
 *
 * @param {string} href Path below the dashboard's base.
 * @param {string} label
 * @param {object} [attrs]
 * @returns {HTMLElement}
 */
export function link(href, label, attrs = {}) {
  return el('a', { href, text: label, ...attrs });
}

/**
 * A monospace cell, for identifiers and paths.
 *
 * @param {string} text
 * @returns {HTMLElement}
 */
export function mono(text) {
  return el('span', { class: 'mono', text: text ?? '—' });
}

/**
 * A card with an optional heading.
 *
 * @param {string | null} heading
 * @param {any} children
 * @returns {HTMLElement}
 */
export function card(heading, children) {
  return el('div', { class: 'card' }, [
    heading ? el('h2', { text: heading, style: 'margin-top:0' }) : null,
    children,
  ]);
}

/**
 * Render a DAG layout as SVG.
 *
 * @param {{nodes: any[], edges: any[], width: number, height: number}} layout
 * @param {object} [options]
 * @param {(node: any) => void} [options.onSelect]
 * @returns {HTMLElement}
 */
export function graphView(layout, { onSelect } = {}) {
  if (!layout?.nodes?.length) return emptyState('This graph has no steps.');

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', String(layout.width));
  svg.setAttribute('height', String(layout.height));
  svg.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `Dependency graph of ${layout.nodes.length} steps`);

  for (const edge of layout.edges) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('class', 'edge');
    path.setAttribute('d', edge.path);
    svg.append(path);
  }

  for (const node of layout.nodes) {
    const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    group.setAttribute('class', 'node');
    group.dataset.tone = node.tone;
    group.dataset.id = node.id;

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', String(node.x));
    rect.setAttribute('y', String(node.y));
    rect.setAttribute('width', String(node.width));
    rect.setAttribute('height', String(node.height));
    group.append(rect);

    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', String(node.x + 10));
    label.setAttribute('y', String(node.y + (node.sublabel ? 19 : 27)));
    label.textContent = truncate(node.label, 20);
    group.append(label);

    if (node.sublabel) {
      const sub = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      sub.setAttribute('class', 'sub');
      sub.setAttribute('x', String(node.x + 10));
      sub.setAttribute('y', String(node.y + 33));
      sub.textContent = truncate(node.sublabel, 26);
      group.append(sub);
    }

    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = node.sublabel ? `${node.label} — ${node.sublabel}` : node.label;
    group.append(title);

    if (onSelect) {
      group.style.cursor = 'pointer';
      group.addEventListener('click', () => onSelect(node));
    }
    svg.append(group);
  }

  return el('div', { class: 'graph' }, svg);
}

/**
 * Render a list of events as a log.
 *
 * @param {Array<{time: string, kind: string, text: string, tone: string}>} lines
 * @param {object} [options]
 * @param {string} [options.empty]
 * @returns {HTMLElement}
 */
export function logView(lines, { empty = 'No events yet.' } = {}) {
  if (!lines.length) return emptyState(empty);
  return el(
    'div',
    { class: 'log', role: 'log', 'aria-live': 'polite' },
    lines.map((line) =>
      el('div', { class: 'log-line', dataset: { tone: line.tone } }, [
        el('span', { class: 'log-time', text: line.time }),
        el('span', { class: 'log-kind', text: line.kind }),
        el('span', { class: 'log-text', text: line.text }),
      ]),
    ),
  );
}

/**
 * Render a duration cell.
 *
 * @param {number | null | undefined} seconds
 * @returns {HTMLElement}
 */
export function duration(seconds) {
  return num(formatDuration(seconds));
}

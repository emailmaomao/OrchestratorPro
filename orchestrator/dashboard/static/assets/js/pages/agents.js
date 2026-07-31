/**
 * Agents: how each role is configured, and what an agent may do.
 *
 * Nothing here names a vendor. The API reports resolved settings — model,
 * effort, budgets — and the dashboard displays them; if the installation is
 * pointed at a self-hosted backend, this page says so without a line of code
 * changing.
 *
 * The prompt preview is the interesting part. It renders exactly what an agent
 * would receive without calling a model, and shows the fingerprint of the
 * cacheable prefix — the number that must not change between two identical
 * requests. When it does, prompt caching has silently stopped happening, and
 * this is the only place an operator can see that before the bill arrives.
 */

import { formatDuration, formatNumber, truncate } from '../format.js';
import {
  badge,
  card,
  el,
  errorState,
  facts,
  loading,
  mono,
  num,
  pageHeader,
  replace,
  table,
} from '../ui.js';

export const agentsPage = {
  title: 'Agents',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ api }) {
    const [roles, tools] = await Promise.all([api.agentRoles(), api.agentTools()]);
    const fragment = document.createDocumentFragment();

    fragment.append(
      pageHeader('Agents', {
        lede:
          'Resolved settings per role, and the tools an agent may call. These ' +
          'describe what a provider would be asked for, not any particular backend.',
      }),
    );

    fragment.append(el('h2', { text: 'Roles' }));
    fragment.append(
      table({
        columns: [
          'Role',
          'Model',
          'Effort',
          'Thinking',
          { label: 'Max tokens', class: 'num' },
          { label: 'Budget', class: 'num' },
        ],
        rows: roles,
        empty: 'No roles are configured.',
        cell: (role) => [
          mono(role.role),
          mono(role.model),
          badge(role.effort, role.effort === 'max' ? 'warn' : 'idle'),
          badge(role.thinking, role.thinking === 'disabled' ? 'warn' : 'ok'),
          num(formatNumber(role.max_tokens)),
          num(
            `${formatDuration(role.budget_seconds)} · ` +
              `${formatNumber(role.budget_tokens)} tok · ` +
              `${formatNumber(role.budget_tool_calls)} calls`,
          ),
        ],
      }),
    );

    fragment.append(el('h2', { text: 'Tools' }));
    fragment.append(
      table({
        columns: ['Tool', 'Description', 'Parameters'],
        rows: tools,
        empty: 'No tools are registered.',
        cell: (tool) => [
          mono(tool.name),
          truncate(tool.description, 90),
          Object.keys(tool.schema?.properties ?? {}).join(', ') || '—',
        ],
      }),
    );

    fragment.append(el('h2', { text: 'Prompt preview' }));
    fragment.append(promptPreview(api));

    return fragment;
  },
};

/**
 * The prompt renderer.
 *
 * @param {import('../api.js').Api} api
 * @returns {HTMLElement}
 */
function promptPreview(api) {
  const title = el('input', {
    type: 'text',
    value: 'Add a greeting',
    'aria-label': 'Task title',
    style: 'min-width:200px',
  });
  const prompt = el('input', {
    type: 'text',
    value: 'Create greeting.txt containing a greeting.',
    'aria-label': 'Task prompt',
    style: 'flex:1;min-width:260px',
  });
  const role = el(
    'select',
    { 'aria-label': 'Agent role' },
    ['worker', 'planner', 'reviewer', 'summarizer'].map((value) =>
      el('option', { value, text: value }),
    ),
  );

  const output = el('div');
  let previous = null;

  const render = async () => {
    replace(output, loading('Rendering…'));
    let rendered;
    try {
      rendered = await api.renderPrompt({
        title: title.value,
        prompt: prompt.value,
        role: role.value,
      });
    } catch (error) {
      replace(output, errorState(error, { onRetry: render }));
      return;
    }

    const key = `${title.value}|${prompt.value}|${role.value}`;
    const stable =
      previous && previous.key === key ? previous.fingerprint === rendered.fingerprint : null;
    previous = { key, fingerprint: rendered.fingerprint };

    replace(output, [
      facts([
        ['Role', rendered.role],
        ['Prefix fingerprint', mono(rendered.fingerprint)],
        [
          'Stability',
          stable === null
            ? 'render the same inputs twice to check'
            : stable
              ? badge('unchanged', 'ok')
              : badge('changed — caching is not happening', 'bad'),
        ],
        ['Tools in the prefix', rendered.tools.join(', ') || 'none'],
      ]),
      el('h2', { text: 'Cached prefix' }),
      ...rendered.blocks.map((block, index) =>
        el('pre', { class: 'log', text: block, 'aria-label': `System block ${index + 1}` }),
      ),
      el('h2', { text: 'Opening turn' }),
      ...rendered.messages.map((message) => el('pre', { class: 'log', text: message })),
    ]);
  };

  return card(null, [
    el('div', { class: 'toolbar' }, [
      title,
      prompt,
      role,
      el('button', { text: 'Render', onClick: render }),
    ]),
    el('p', {
      class: 'lede',
      text:
        'Renders what an agent would receive. No model is called. Retry feedback ' +
        'is deliberately outside the cached prefix — it changes every attempt, and ' +
        'in the prefix it would invalidate the cache each time.',
    }),
    output,
  ]);
}

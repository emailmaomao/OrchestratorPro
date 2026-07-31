/**
 * The live event viewer.
 *
 * Every event, from every run, as it is recorded. This is the log viewer for
 * the system as a whole; a run's own log lives on its detail page.
 *
 * It shares the application's single event stream rather than opening its own,
 * and it holds a bounded buffer. An event viewer that grows without limit is a
 * tab that has to be closed after an hour, which means it is a tab nobody
 * leaves open, which defeats the point of having one.
 */

import { describeEvent, shortId, toneForEventType } from '../format.js';
import { badge, card, el, emptyState, facts, logView, pageHeader, replace } from '../ui.js';

/** How many events the viewer keeps. Older ones are in the run's own log. */
const BUFFER = 400;

export const eventsPage = {
  title: 'Events',

  /**
   * @param {object} ctx
   * @returns {Promise<Node>}
   */
  async render({ hub, onCleanup }) {
    const fragment = document.createDocumentFragment();
    const buffer = hub.recent.slice(-BUFFER);

    const filter = el('input', {
      type: 'search',
      placeholder: 'Filter by type, run, or text',
      'aria-label': 'Filter events',
      style: 'flex:1;min-width:220px',
    });
    const follow = el('input', { type: 'checkbox', checked: true });
    const status = el('span', { class: 'lede', style: 'margin:0' });
    const stream = el('div');

    /** Whether one event passes the current filter. */
    const matches = (event) => {
      const needle = filter.value.trim().toLowerCase();
      if (!needle) return true;
      const line = describeEvent(event);
      return (
        line.kind.toLowerCase().includes(needle) ||
        line.text.toLowerCase().includes(needle) ||
        (event.run_id ?? '').toLowerCase().includes(needle)
      );
    };

    /** Repaint the visible window. */
    const paint = () => {
      const visible = buffer.filter(matches);
      replace(
        stream,
        logView(visible.map(describeEvent), {
          empty: buffer.length
            ? 'No events match that filter.'
            : 'Waiting for events. Start a run and they will appear here.',
        }),
      );
      status.textContent =
        `${visible.length} of ${buffer.length} shown` +
        (hub.connected ? '' : ' · stream interrupted, reconnecting');
      if (follow.checked) {
        const log = stream.querySelector('.log');
        if (log) log.scrollTop = log.scrollHeight;
      }
    };

    filter.addEventListener('input', paint);

    fragment.append(
      pageHeader('Events', {
        lede:
          'The live log, as the server records it. Nothing here is derived: ' +
          'these are the entries a replay would read back.',
      }),
    );

    fragment.append(
      card(null, [
        el('div', { class: 'toolbar' }, [
          filter,
          el('label', { class: 'check' }, [follow, 'Follow']),
          el('button', {
            text: 'Clear',
            onClick: () => {
              buffer.length = 0;
              paint();
            },
          }),
          status,
        ]),
        stream,
      ]),
    );

    fragment.append(el('h2', { text: 'Recent activity by type' }));
    const breakdown = el('div');
    fragment.append(breakdown);

    /** Count events by type, so a burst of one kind is visible at a glance. */
    const paintBreakdown = () => {
      const counts = new Map();
      for (const event of buffer) {
        counts.set(event.type, (counts.get(event.type) ?? 0) + 1);
      }
      if (!counts.size) {
        replace(breakdown, emptyState('Nothing recorded yet.'));
        return;
      }
      replace(
        breakdown,
        el(
          'div',
          { class: 'toolbar' },
          [...counts.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([type, count]) => badge(`${type} · ${count}`, toneForEventType(type))),
        ),
      );
    };

    paint();
    paintBreakdown();

    let scheduled = null;
    const unsubscribe = hub.subscribe((event) => {
      if (!event?.id) {
        // A stream notice, not a recorded event. It changes the status line
        // and nothing else.
        status.textContent = 'stream interrupted, reconnecting…';
        return;
      }
      buffer.push(event);
      while (buffer.length > BUFFER) buffer.shift();
      clearTimeout(scheduled);
      scheduled = setTimeout(() => {
        paint();
        paintBreakdown();
      }, 100);
    });

    onCleanup(() => {
      clearTimeout(scheduled);
      unsubscribe();
    });

    fragment.append(
      card('About this stream', [
        facts([
          ['Transport', 'Server-Sent Events'],
          ['Buffer', `${BUFFER} events`],
          [
            'Scope',
            'every run — a single run’s log is on its own page, and goes back further',
          ],
          ['Last run seen', shortId(buffer.at(-1)?.run_id) ?? '—'],
        ]),
      ]),
    );

    return fragment;
  },
};

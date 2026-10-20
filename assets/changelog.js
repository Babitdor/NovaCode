/* Progressive enhancement: the embedded archive works without JavaScript or GitHub. */
(() => {
  'use strict';
  const releases = document.getElementById('releases');
  const status = document.getElementById('release-status');
  const api = 'https://api.github.com/repos/Babitdor/NovaCode/contents/changelog?ref=main';
  const rawBase = 'https://raw.githubusercontent.com/Babitdor/NovaCode/main/changelog/';
  const filename = /^CHANGELOG-v(\d+\.\d+\.\d+)\.md$/;
  const compare = (a, b) => {
    const av = a.split('.').map(Number), bv = b.split('.').map(Number);
    for (let i = 0; i < 3; i++) if (av[i] !== bv[i]) return bv[i] - av[i];
    return 0;
  };
  const escape = text => text.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const inline = text => escape(text).replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  function openHash() {
    const match = location.hash.match(/^#v(\d+)[.-](\d+)[.-](\d+)$/);
    if (!match) return;
    const entry = document.getElementById('v' + match.slice(1).join('-'));
    if (!entry) return;
    entry.open = true;
    entry.scrollIntoView({ behavior: 'instant', block: 'start' });
  }
  window.addEventListener('hashchange', openHash);
  openHash();

  function render(version, markdown, sha) {
    // Render only headings, lists and inline emphasis. Raw HTML is always escaped.
    const blocks = [];
    let items = [], heading = '', summary = '';
    function flush() {
      if (heading) blocks.push('<h3>' + inline(heading) + '</h3>');
      if (items.length) {
        if (!summary) summary = items[0].replace(/\*\*|`/g, '');
        blocks.push('<ul>' + items.map(item => '<li>' + inline(item) + '</li>').join('') + '</ul>');
      }
      heading = ''; items = [];
    }
    for (const line of markdown.split(/\r?\n/)) {
      const value = line.trim();
      if (/^#{2,}\s/.test(value)) {
        flush(); heading = value.replace(/^#+\s*/, '');
      } else if (/^[-*]\s/.test(value)) {
        items.push(value.slice(2));
      } else if (value && !value.startsWith('#') && items.length) {
        items[items.length - 1] += ' ' + value;
      }
    }
    flush();
    const id = 'v' + version.replace(/\./g, '-');
    const entry = document.createElement('details');
    entry.className = 'changelog-entry';
    entry.id = id;
    entry.dataset.version = version;
    entry.dataset.sha = sha;
    entry.innerHTML = '<summary class="entry-toggle"><h2 class="version">v' + version + '</h2>' +
      '<span class="release-summary">' + escape(summary || 'Release v' + version) + '</span></summary>' +
      '<div class="release-content"><p><a href="#' + id + '">Link to v' + version + '</a></p>' + blocks.join('') + '</div>';
    return entry;
  }

  async function request(url, type) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, { signal: controller.signal, cache: 'no-cache' });
      if (!response.ok) throw new Error('Changelog source unavailable');
      return await response[type]();
    } finally { clearTimeout(timeout); }
  }

  async function refresh() {
    if (!/^https?:$/.test(location.protocol)) return;
    status.textContent = 'Checking for newer releases…';
    try {
      const listing = await request(api, 'json');
      if (!Array.isArray(listing)) throw new Error('Invalid release listing');
      const files = listing.filter(file => file.type === 'file' && filename.test(file.name));
      if (!files.length) throw new Error('Empty release listing');
      const changed = files.filter(file => {
        const version = file.name.match(filename)[1];
        return document.getElementById('v' + version.replace(/\./g, '-'))?.dataset.sha !== file.sha;
      });
      let failures = 0;
      const queue = changed.slice();
      // Keep network concurrency bounded even when several releases have changed.
      await Promise.all(Array.from({ length: Math.min(4, queue.length) }, async () => {
        while (queue.length) {
          const file = queue.shift();
          const version = file.name.match(filename)[1];
          try {
            const markdown = await request(rawBase + encodeURIComponent(file.name), 'text');
            if (!markdown.trim() || !/^#\s/m.test(markdown)) throw new Error('Invalid release');
            const entry = render(version, markdown, file.sha);
            const previous = document.getElementById(entry.id);
            if (previous) {
              entry.open = previous.open;
              if (previous.contains(document.activeElement)) continue;
              previous.replaceWith(entry);
            } else {
              const before = [...releases.children].find(other => compare(version, other.dataset.version) < 0);
              releases.insertBefore(entry, before || null);
            }
          } catch { failures++; }
        }
      }));
      const versions = [...releases.querySelectorAll('.changelog-entry')].map(entry => entry.dataset.version).sort(compare);
      document.querySelector('.lede').textContent = versions.length + ' releases · v' + versions.at(-1) +
        ' → v' + versions[0] + ' · from the repository’s changelog/ directory';
      status.textContent = failures ? 'Some updates could not load. The saved archive is still available.' : 'Up to date with the repository.';
      if (changed.length && !failures) openHash();
    } catch {
      status.textContent = 'Live updates are unavailable. Showing the saved release archive.';
    }
  }
  refresh();
})();

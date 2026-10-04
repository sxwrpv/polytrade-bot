const groups = [
  { title: 'Getting Started', pages: [
    ['overview', 'Overview', 'README.md'],
    ['getting-started', 'Quickstart', 'getting-started.md'],
  ]},
  { title: 'Core Concepts', pages: [
    ['core-concepts', 'How PolyTrade Works', 'core-concepts.md'],
    ['copy-trading', 'Copy Trading', 'copy-trading.md'],
    ['wallet-and-funding', 'Wallet & Funding', 'wallet-and-funding.md'],
  ]},
  { title: 'Risk & Safety', pages: [
    ['risk-and-security', 'Risk and Security', 'risk-and-security.md'],
  ]},
  { title: 'Developers', pages: [
    ['developers', 'Developers Hub', 'developers.md'],
    ['api-reference', 'API Reference', 'api-reference.md'],
    // Rendered from an HTML fragment rather than Markdown: it is a collection
    // of inline SVG diagrams, which the Markdown renderer would escape.
    ['system-design', 'System Design', 'system-design.html', 'html'],
  ]},
  { title: 'Operations', pages: [
    ['operators', 'Operators Hub', 'operators.md'],
    ['configuration', 'Configuration', 'configuration.md'],
    ['deployment', 'Deployment', 'deployment.md'],
    ['troubleshooting', 'Troubleshooting', 'troubleshooting.md'],
  ]},
  { title: 'Reference', pages: [
    ['glossary', 'Glossary', 'glossary.md'],
    ['links', 'Official Links', 'links.md'],
  ]},
];

const pages = groups.flatMap(group => group.pages.map(page => ({
  slug: page[0], title: page[1], file: page[2], kind: page[3] || 'markdown',
  group: group.title,
})));
const pageMap = Object.fromEntries(pages.map(page => [page.slug, page]));

function currentSlug() {
  const path = window.location.pathname.replace(/^\/docs\/?/, '').replace(/\/$/, '');
  return pageMap[path] ? path : 'overview';
}

function pageUrl(slug) {
  return slug === 'overview' ? '/docs' : `/docs/${slug}`;
}

function escapeHtml(value) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function slugify(value) {
  return value.toLowerCase().replace(/<[^>]*>/g, '').replace(/[^a-z0-9\s-]/g, '').trim().replace(/\s+/g, '-');
}

function normalizeLink(target) {
  const value = target.trim();
  if (/^https?:\/\//i.test(value) || /^mailto:/i.test(value) || value.startsWith('#')) {
    return value;
  }
  // Reject protocol-relative URLs and every non-allowlisted scheme, including
  // javascript: and data:. Documentation Markdown is rendered with innerHTML.
  if (value.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(value)) return '#';
  const [path, anchor] = value.split('#');
  const file = path.split('/').pop();
  const page = pages.find(item => item.file === file);
  if (!page) return '#';
  return `${pageUrl(page.slug)}${anchor ? `#${anchor}` : ''}`;
}

function inline(value) {
  let output = escapeHtml(value);
  output = output.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, target) => {
    const href = normalizeLink(target);
    const external = /^https?:\/\//i.test(href);
    return `<a href="${escapeAttribute(href)}"${external ? ' target="_blank" rel="noreferrer"' : ''}>${label}</a>`;
  });
  output = output.replace(/`([^`]+)`/g, '<code>$1</code>');
  output = output.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  output = output.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return output;
}

function renderMarkdown(markdown) {
  const lines = markdown.replace(/\r/g, '').split('\n');
  const out = [];
  let paragraph = [];
  let list = null;
  let code = null;
  let quote = [];

  const flushParagraph = () => {
    if (paragraph.length) out.push(`<p>${inline(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!list) return;
    const cls = list.checklist ? ' class="checklist"' : '';
    out.push(`<${list.type}${cls}>${list.items.map(item => `<li>${inline(item)}</li>`).join('')}</${list.type}>`);
    list = null;
  };
  const flushQuote = () => {
    if (!quote.length) return;
    const warning = quote[0].match(/^\[!WARNING\]/i);
    if (warning) {
      const body = quote.join(' ').replace(/^\[!WARNING\]\s*/i, '');
      out.push(`<aside class="callout"><div class="callout-title">⚠ Warning</div><div>${inline(body)}</div></aside>`);
    } else {
      out.push(`<blockquote><p>${inline(quote.join(' '))}</p></blockquote>`);
    }
    quote = [];
  };
  const flushAll = () => { flushParagraph(); flushList(); flushQuote(); };

  for (const line of lines) {
    if (code) {
      if (line.startsWith('```')) {
        out.push(`<pre><code>${escapeHtml(code.lines.join('\n'))}</code></pre>`);
        code = null;
      } else code.lines.push(line);
      continue;
    }
    if (line.startsWith('```')) {
      flushAll();
      code = { lang: line.slice(3).trim(), lines: [] };
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      const label = inline(heading[2]);
      const id = slugify(heading[2]);
      out.push(`<h${level} id="${id}"><a class="heading-anchor" href="#${id}">${label}</a></h${level}>`);
      continue;
    }
    const quoted = line.match(/^>\s?(.*)$/);
    if (quoted) {
      flushParagraph(); flushList();
      quote.push(quoted[1]);
      continue;
    }
    const unordered = line.match(/^\s*-\s+(.*)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.*)$/);
    if (unordered || ordered) {
      flushParagraph(); flushQuote();
      const type = ordered ? 'ol' : 'ul';
      let item = (unordered || ordered)[1];
      const checked = /^\[[ xX]\]\s*/.test(item);
      item = item.replace(/^\[[ xX]\]\s*/, '');
      if (!list || list.type !== type) { flushList(); list = { type, items: [], checklist: checked }; }
      list.checklist ||= checked;
      list.items.push(item);
      continue;
    }
    if (!line.trim()) { flushAll(); continue; }
    flushList(); flushQuote();
    paragraph.push(line.trim());
  }
  flushAll();
  if (code) out.push(`<pre><code>${escapeHtml(code.lines.join('\n'))}</code></pre>`);
  return out.join('\n');
}

function renderSidebar(activeSlug) {
  document.getElementById('sidebar-nav').innerHTML = groups.map(group => `
    <section class="nav-group">
      <h2 class="nav-group-title">${group.title}</h2>
      ${group.pages.map(([slug, title]) => `<a class="nav-link${slug === activeSlug ? ' active' : ''}" href="${pageUrl(slug)}"${slug === activeSlug ? ' aria-current="page"' : ''}>${title}</a>`).join('')}
    </section>
  `).join('');
}

function renderToc() {
  // Bilingual pages hold both languages in the DOM at once; only list the
  // headings currently on screen, or the contents would show every entry twice.
  const headings = [...document.querySelectorAll('.prose h2, .prose h3')]
    .filter(heading => heading.offsetParent !== null || !heading.className);
  const nav = document.getElementById('toc-nav');
  nav.innerHTML = headings.map(h => `<a href="#${h.id}" data-id="${h.id}"${h.tagName === 'H3' ? ' style="padding-left:22px"' : ''}>${h.textContent}</a>`).join('');
  if (!headings.length) document.querySelector('.toc').style.display = 'none';

  const observer = new IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (!visible) return;
    nav.querySelectorAll('a').forEach(a => a.classList.toggle('active', a.dataset.id === visible.target.id));
  }, { rootMargin: '-110px 0px -70% 0px' });
  headings.forEach(heading => observer.observe(heading));
}

function renderPager(slug) {
  const index = pages.findIndex(page => page.slug === slug);
  const previous = pages[index - 1];
  const next = pages[index + 1];
  document.getElementById('page-pager').innerHTML = `
    ${previous ? `<a class="pager-link" href="${pageUrl(previous.slug)}"><span class="pager-label">← Previous</span><span class="pager-title">${previous.title}</span></a>` : '<span></span>'}
    ${next ? `<a class="pager-link next" href="${pageUrl(next.slug)}"><span class="pager-label">Next →</span><span class="pager-title">${next.title}</span></a>` : ''}
  `;
}

/* Documentation language. Russian pages live beside the English ones as
   `<name>.ru.md`; a page without a translation falls back to English rather
   than failing, so the switch never leaves the reader on an error screen. */
let docsLang = localStorage.getItem('polytrade-docs-lang') === 'ru' ? 'ru' : 'en';

async function fetchDoc(file, kind = 'markdown') {
  if (kind === 'html') {
    // One bilingual file; the language toggle picks which spans are visible,
    // so there is no `.ru.html` variant to fall back to.
    const response = await fetch(`/docs/assets/${file}`, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`Documentation returned ${response.status}`);
    return response.text();
  }
  if (docsLang === 'ru') {
    const translated = await fetch(`/docs/content/${file.replace(/\.md$/, '.ru.md')}`, { cache: 'no-cache' });
    if (translated.ok) return translated.text();
  }
  const response = await fetch(`/docs/content/${file}`, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`Documentation returned ${response.status}`);
  return response.text();
}

async function loadPage() {
  const slug = currentSlug();
  const page = pageMap[slug];
  renderSidebar(slug);
  // Diagram-heavy pages get a wider content column and narrower rails. Set on
  // the shell rather than the prose, because the column widths are grid tracks.
  document.querySelector('.docs-shell')?.classList.toggle('is-wide', slug === 'system-design');
  try {
    const source = await fetchDoc(page.file, page.kind);
    const prose = document.getElementById('prose');
    prose.classList.toggle('lang-ru', docsLang === 'ru');
    prose.innerHTML = page.kind === 'html' ? source : renderMarkdown(source);
    prose.hidden = false;
    if (page.kind === 'html') {
      // Behaviour is optional: if the module fails to load, the diagrams are
      // still fully rendered and readable.
      import('/docs/assets/system-design.js')
        .then(module => module.setupSystemDesign(prose))
        .catch(() => {});
    }
    document.getElementById('loading-state').hidden = true;
    document.title = `${page.title} - PolyTrade Documentation`;
    renderToc();
    renderPager(slug);
  } catch (error) {
    const prose = document.getElementById('prose');
    prose.innerHTML = `<h1>Documentation unavailable</h1><p>${escapeHtml(error.message)}</p><p><a href="/docs">Return to the documentation overview</a>.</p>`;
    prose.hidden = false;
    document.getElementById('loading-state').hidden = true;
  }
}

const searchData = new Map();
async function buildSearchIndex() {
  await Promise.all(pages.map(async page => {
    try {
      const raw = await fetchDoc(page.file, page.kind);
      const text = page.kind === 'html'
        ? raw.replace(/<(script|style|svg)[\s\S]*?<\/\1>/gi, ' ').replace(/<[^>]*>/g, ' ')
        : raw.replace(/```[\s\S]*?```/g, ' ').replace(/[#>*_`\[\]()-]/g, ' ');
      searchData.set(page.slug, text.replace(/\s+/g, ' ').trim());
    } catch { searchData.set(page.slug, ''); }
  }));
}

let selectedResult = 0;
function search(query) {
  const container = document.getElementById('search-results');
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const matches = !terms.length ? pages.slice(0, 6) : pages.filter(page => {
    const haystack = `${page.title} ${page.group} ${searchData.get(page.slug) || ''}`.toLowerCase();
    return terms.every(term => haystack.includes(term));
  }).slice(0, 8);
  selectedResult = 0;
  if (!matches.length) { container.innerHTML = '<div class="search-empty">No documentation matched your search.</div>'; return; }
  container.innerHTML = matches.map((page, index) => {
    const copy = searchData.get(page.slug) || 'Open this documentation page.';
    return `<a class="search-result${index === 0 ? ' selected' : ''}" href="${pageUrl(page.slug)}"><span class="search-result-group">${page.group}</span><div class="search-result-title">${page.title}</div><div class="search-result-copy">${escapeHtml(copy.slice(0, 150))}</div></a>`;
  }).join('');
}

function openSearch() {
  const modal = document.getElementById('search-modal');
  modal.hidden = false;
  document.body.style.overflow = 'hidden';
  const input = document.getElementById('search-input');
  input.value = '';
  search('');
  setTimeout(() => input.focus(), 0);
}
function closeSearch() {
  document.getElementById('search-modal').hidden = true;
  document.body.style.overflow = '';
}

function setupInteractions() {
  const sidebar = document.getElementById('sidebar');
  const menu = document.getElementById('mobile-menu');
  menu.addEventListener('click', () => {
    const open = sidebar.classList.toggle('open');
    menu.setAttribute('aria-expanded', String(open));
  });
  document.getElementById('sidebar-backdrop').addEventListener('click', () => {
    sidebar.classList.remove('open'); menu.setAttribute('aria-expanded', 'false');
  });

  const langButton = document.getElementById('lang-toggle');
  langButton.textContent = docsLang === 'ru' ? 'EN' : 'RU';
  langButton.addEventListener('click', () => {
    docsLang = docsLang === 'ru' ? 'en' : 'ru';
    localStorage.setItem('polytrade-docs-lang', docsLang);
    langButton.textContent = docsLang === 'ru' ? 'EN' : 'RU';
    document.documentElement.lang = docsLang;
    searchData.clear();
    loadPage();
  });

  document.getElementById('search-trigger').addEventListener('click', openSearch);
  document.querySelectorAll('[data-close-search]').forEach(button => button.addEventListener('click', closeSearch));
  document.getElementById('search-input').addEventListener('input', event => search(event.target.value));
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openSearch(); }
    if (event.key === 'Escape' && !document.getElementById('search-modal').hidden) closeSearch();
    if (!document.getElementById('search-modal').hidden && ['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) {
      const results = [...document.querySelectorAll('.search-result')];
      if (!results.length) return;
      event.preventDefault();
      if (event.key === 'Enter') { window.location.href = results[selectedResult].href; return; }
      selectedResult = (selectedResult + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
      results.forEach((result, index) => result.classList.toggle('selected', index === selectedResult));
      results[selectedResult].scrollIntoView({ block: 'nearest' });
    }
  });
}

setupInteractions();
buildSearchIndex();
loadPage();

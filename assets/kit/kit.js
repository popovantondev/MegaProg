/* popovantondev kit: theme button, floating header, search palette (Ctrl+K and "/"),
   typed terminal, reveal on scroll, localhost link rewrite, tab keyboard support.
   The pages work without it; it only improves them. Needs programs.js for the palette. */
(function () {
  'use strict';
  var doc = document, root = doc.documentElement;
  var ORIGIN = 'https://popovantondev.github.io';
  var KEY = 'ppk-theme';
  var STR = {
    de: { label: 'Programme durchsuchen', ph: 'Programm oder Aufgabe suchen …', none: 'Nichts gefunden.', select: 'wählen', open: 'öffnen', close: 'schließen', closeBtn: 'Schließen', count: ' Ergebnisse' },
    en: { label: 'Search programs', ph: 'Search a program or task …', none: 'Nothing found.', select: 'select', open: 'open', close: 'close', closeBtn: 'Close', count: ' results' },
    ru: { label: 'Поиск по программам', ph: 'Найти программу или задачу …', none: 'Ничего не найдено.', select: 'выбрать', open: 'открыть', close: 'закрыть', closeBtn: 'Закрыть', count: ' результатов' }
  };

  function pageLang() {
    var l = (root.getAttribute('lang') || 'de').slice(0, 2).toLowerCase();
    return STR[l] ? l : 'en';
  }
  function isLocal() {
    var h = location.hostname;
    return h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h === '::1';
  }
  function local(url) {
    return isLocal() && url.indexOf(ORIGIN) === 0 ? location.origin + url.slice(ORIGIN.length) : url;
  }
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function reduced() {
    return !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* ---------- theme ---------- */
  function stored() {
    try { var v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; } catch (e) { return null; }
  }
  function systemTheme() {
    return window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function setTheme(t, remember) {
    root.setAttribute('data-theme', t);
    if (remember) { try { localStorage.setItem(KEY, t); } catch (e) {} }
  }
  function initTheme() {
    if (!root.getAttribute('data-theme')) setTheme(stored() || systemTheme(), false);
    doc.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-ppk-theme]');
      if (!b) return;
      setTheme(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark', true);
    });
    if (window.matchMedia) {
      var mq = matchMedia('(prefers-color-scheme: dark)');
      var follow = function () { if (!stored()) setTheme(systemTheme(), false); };
      if (mq.addEventListener) mq.addEventListener('change', follow); else if (mq.addListener) mq.addListener(follow);
    }
    window.addEventListener('storage', function (e) {
      if (e.key === KEY) setTheme(stored() || systemTheme(), false);
    });
  }

  /* ---------- floating header ---------- */
  function initNav() {
    var wrap = doc.getElementById('ppk-nav-wrap');
    if (!wrap) return;
    var ticking = false;
    function update() { ticking = false; wrap.classList.toggle('ppk-floating', (window.pageYOffset || root.scrollTop) > 160); }
    window.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    update();
  }

  /* ---------- localhost links ---------- */
  function rewriteLinks(scope) {
    if (!isLocal()) return;
    var links = (scope || doc).querySelectorAll('a[href^="' + ORIGIN + '"]');
    for (var i = 0; i < links.length; i++) links[i].setAttribute('href', local(links[i].getAttribute('href')));
  }

  /* ---------- search palette ---------- */
  var programs = window.PPK_PROGRAMS || [];
  var categories = window.PPK_CATEGORIES || {};
  var lang = pageLang();
  var S = STR[lang];
  var index = null, ui = null, results = [], sel = 0, lastFocus = null;

  function norm(s) {
    s = String(s == null ? '' : s).toLowerCase().replace(/ß/g, 'ss');
    try { s = s.normalize('NFD').replace(/[\u0300-\u036f]/g, ''); } catch (e) {}
    return s;
  }
  function catLabel(p, l) { var c = categories[p.category]; return c ? (c[l] || c.de) : ''; }
  function buildIndex() {
    index = programs.map(function (p) {
      var cat = catLabel(p, lang);
      return {
        p: p, cat: cat,
        name: norm(p.name + ' ' + p.repo),
        hay: norm([p.name, p.repo, p.desc && p.desc[lang], p.platform, p.stack, p.category, cat].join(' '))
      };
    });
  }
  function search(q) {
    var tokens = norm(q).split(/\s+/).filter(Boolean);
    var hits = index.filter(function (e) { return tokens.every(function (t) { return e.hay.indexOf(t) !== -1; }); });
    if (tokens.length) {
      var rank = function (e) { return tokens.every(function (t) { return e.name.indexOf(t) !== -1; }) ? 0 : 1; };
      hits = hits.map(function (e, i) { return { e: e, i: i, r: rank(e) }; })
        .sort(function (a, b) { return a.r - b.r || a.i - b.i; })
        .map(function (x) { return x.e; });
    }
    return hits;
  }

  function buildPalette() {
    var overlay = el('div', 'ppk-overlay'); overlay.id = 'ppk-overlay'; overlay.hidden = true;
    var dialog = el('div', 'ppk-palette');
    dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', S.label);
    var head = el('div', 'ppk-phead');
    var input = el('input', 'ppk-palette-input');
    input.type = 'text'; input.id = 'ppk-pq'; input.placeholder = S.ph; input.autocomplete = 'off'; input.spellcheck = false;
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-expanded', 'true'); input.setAttribute('aria-controls', 'ppk-plist');
    input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-label', S.label); input.setAttribute('enterkeyhint', 'go');
    var closeBtn = el('button', 'ppk-pclose', 'Esc'); closeBtn.type = 'button'; closeBtn.setAttribute('aria-label', S.closeBtn);
    head.appendChild(input); head.appendChild(closeBtn);
    var list = el('ul', 'ppk-plist'); list.id = 'ppk-plist'; list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', S.label);
    var empty = el('p', 'ppk-pempty', S.none); empty.setAttribute('role', 'status'); empty.hidden = true;
    var foot = el('div', 'ppk-pfoot');
    [['↑ ↓', S.select], ['Enter', S.open], ['Esc', S.close]].forEach(function (k) {
      var s = el('span'); s.appendChild(el('kbd', 'ppk-kbd', k[0])); s.appendChild(doc.createTextNode(' ' + k[1])); foot.appendChild(s);
    });
    var live = el('div', 'ppk-sr-only'); live.setAttribute('aria-live', 'polite');
    dialog.appendChild(head); dialog.appendChild(list); dialog.appendChild(empty); dialog.appendChild(foot); dialog.appendChild(live);
    overlay.appendChild(dialog);
    doc.body.appendChild(overlay);

    overlay.addEventListener('mousedown', function (e) { if (e.target === overlay) closePalette(); });
    closeBtn.addEventListener('click', closePalette);
    input.addEventListener('input', function () { sel = 0; render(); });
    list.addEventListener('mousemove', function (e) {
      var a = e.target.closest && e.target.closest('.ppk-pitem');
      if (a && +a.getAttribute('data-i') !== sel) select(+a.getAttribute('data-i'), false);
    });
    overlay.addEventListener('keydown', onPaletteKey);
    list.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('.ppk-pitem');
      if (a && !(e.ctrlKey || e.metaKey || e.shiftKey || e.button)) leavePalette();
    });
    return { overlay: overlay, input: input, list: list, empty: empty, live: live, closeBtn: closeBtn };
  }

  function render() {
    results = search(ui.input.value);
    ui.list.textContent = '';
    ui.empty.hidden = results.length > 0;
    ui.list.hidden = results.length === 0;
    results.forEach(function (e, i) {
      var p = e.p;
      var li = el('li'); li.setAttribute('role', 'presentation');
      var a = el('a', 'ppk-pitem'); a.id = 'ppk-opt-' + i; a.href = local(p.url[lang] || p.url.de); a.tabIndex = -1;
      a.setAttribute('role', 'option'); a.setAttribute('data-i', i);
      var img = el('img'); img.alt = ''; img.width = 30; img.height = 30; img.loading = 'lazy'; img.src = local(p.icon);
      img.onerror = function () { var l = el('span', 'ppk-pletter', (p.name || '?').charAt(0).toUpperCase()); l.setAttribute('aria-hidden', 'true'); if (img.parentNode) img.parentNode.replaceChild(l, img); };
      var text = el('span', 'ppk-ptext');
      text.appendChild(el('span', 'ppk-pname', p.name));
      text.appendChild(el('small', null, [e.cat, p.platform, p.stack].filter(Boolean).join(' · ')));
      a.appendChild(img); a.appendChild(text); li.appendChild(a); ui.list.appendChild(li);
    });
    ui.live.textContent = results.length ? results.length + S.count : S.none;
    select(Math.min(sel, Math.max(0, results.length - 1)), true);
  }

  function select(i, scroll) {
    sel = i;
    var items = ui.list.querySelectorAll('.ppk-pitem');
    for (var k = 0; k < items.length; k++) items[k].setAttribute('aria-selected', k === i ? 'true' : 'false');
    if (items[i]) {
      ui.input.setAttribute('aria-activedescendant', items[i].id);
      if (scroll && items[i].scrollIntoView) items[i].scrollIntoView({ block: 'nearest' });
    } else {
      ui.input.removeAttribute('aria-activedescendant');
    }
  }

  function isOpen() { return !!ui && !ui.overlay.hidden; }
  function openPalette() {
    if (!programs.length) return;
    if (!index) buildIndex();
    if (!ui) ui = buildPalette();
    if (isOpen()) return;
    lastFocus = doc.activeElement;
    ui.overlay.hidden = false;
    root.classList.add('ppk-lock');
    ui.input.value = ''; sel = 0; render();
    ui.input.focus();
  }
  function closePalette() {
    if (!isOpen()) return;
    ui.overlay.hidden = true;
    root.classList.remove('ppk-lock');
    var back = lastFocus && doc.contains(lastFocus) ? lastFocus : doc.querySelector('[data-ppk-open-search]');
    if (back && back.focus) back.focus();
  }

  /* Leaving the page from the palette: hide it and unlock scrolling first, so a page restored from the
     back/forward cache never comes back with an open palette and a frozen scroll. */
  function leavePalette() {
    if (ui) ui.overlay.hidden = true;
    root.classList.remove('ppk-lock');
  }
  window.addEventListener('pageshow', function (e) { if (e.persisted) leavePalette(); });
  window.addEventListener('pagehide', leavePalette);

  function onPaletteKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); if (results.length) select(Math.min(sel + 1, results.length - 1), true); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (results.length) select(Math.max(sel - 1, 0), true); }
    else if (e.key === 'Enter' && e.target === ui.input) {
      var a = ui.list.querySelector('.ppk-pitem[aria-selected="true"]');
      if (a) { e.preventDefault(); leavePalette(); location.href = a.href; }
    } else if (e.key === 'Tab') {
      var first = ui.input, last = ui.closeBtn;
      if (e.shiftKey && doc.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && doc.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function typingTarget(t) {
    if (!t || !t.tagName) return false;
    var n = t.tagName.toLowerCase();
    return n === 'input' || n === 'textarea' || n === 'select' || t.isContentEditable;
  }
  /* A page that needs "/" for itself (the piano: Shift+7 on a German keyboard) sets data-ppk-slash="off" on <body>. */
  function slashOff() {
    return !!(doc.body && doc.body.getAttribute('data-ppk-slash') === 'off');
  }
  function initPalette() {
    var buttons = doc.querySelectorAll('[data-ppk-open-search]');
    if (!programs.length) { for (var i = 0; i < buttons.length; i++) buttons[i].hidden = true; return; }
    doc.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-ppk-open-search]');
      if (b) { e.preventDefault(); openPalette(); }
    });
    doc.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K' || e.code === 'KeyK')) {
        e.preventDefault(); if (isOpen()) closePalette(); else openPalette();
      } else if (e.key === '/' && !slashOff() && !e.ctrlKey && !e.metaKey && !e.altKey && !isOpen() && !typingTarget(doc.activeElement)) {
        e.preventDefault(); openPalette();
      }
    });
    doc.addEventListener('focusin', function (e) {
      if (isOpen() && !ui.overlay.contains(e.target)) ui.input.focus();
    });
  }

  /* ---------- typed terminal ---------- */
  function initTerminals() {
    var terms = doc.querySelectorAll('.ppk-term-out');
    if (!terms.length || reduced() || !('IntersectionObserver' in window)) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { io.unobserve(en.target); typeTerminal(en.target); }
      });
    }, { threshold: 0.4 });
    for (var i = 0; i < terms.length; i++) io.observe(terms[i]);
  }
  function typeTerminal(out) {
    var rows = Array.prototype.map.call(out.children, function (c) { return { cls: c.className, text: c.textContent }; });
    if (!rows.length) return;
    var copy = el('div', 'ppk-sr-only', rows.map(function (r) { return r.text; }).join('\n'));
    out.parentNode.insertBefore(copy, out);
    out.setAttribute('aria-hidden', 'true');
    out.style.minHeight = out.offsetHeight + 'px';
    out.textContent = '';
    var delay = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    (async function () {
      for (var i = 0; i < rows.length; i++) {
        var line = el('div', rows[i].cls); out.appendChild(line);
        if (/\bppk-ps\b/.test(rows[i].cls)) {
          for (var k = 1; k <= rows[i].text.length; k++) {
            line.textContent = rows[i].text.slice(0, k);
            line.appendChild(el('span', 'ppk-cursor'));
            await delay(22);
          }
        }
        line.textContent = rows[i].text;
        await delay(420);
      }
    })();
  }

  /* ---------- reveal on scroll ---------- */
  function initReveal() {
    var items = doc.querySelectorAll('.ppk-reveal');
    if (!items.length) return;
    if (!('IntersectionObserver' in window) || reduced()) {
      for (var i = 0; i < items.length; i++) items[i].classList.add('ppk-in');
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add('ppk-in'); io.unobserve(en.target); } });
    }, { threshold: 0.12 });
    for (var j = 0; j < items.length; j++) io.observe(items[j]);
  }

  /* ---------- tabs: roving focus and arrow keys (filtering stays with the page) ---------- */
  function initTabs() {
    var lists = doc.querySelectorAll('.ppk-tabs[role="tablist"]');
    Array.prototype.forEach.call(lists, function (list) {
      var tabs = function () { return Array.prototype.slice.call(list.querySelectorAll('[role="tab"]')); };
      var sync = function () { tabs().forEach(function (t) { t.tabIndex = t.getAttribute('aria-selected') === 'true' ? 0 : -1; }); };
      sync();
      if (window.MutationObserver) new MutationObserver(sync).observe(list, { attributes: true, subtree: true, attributeFilter: ['aria-selected'] });
      list.addEventListener('click', function () { setTimeout(sync, 0); });
      list.addEventListener('keydown', function (e) {
        var all = tabs(), i = all.indexOf(doc.activeElement), n = -1;
        if (i < 0) return;
        if (e.key === 'ArrowRight') n = (i + 1) % all.length;
        else if (e.key === 'ArrowLeft') n = (i - 1 + all.length) % all.length;
        else if (e.key === 'Home') n = 0;
        else if (e.key === 'End') n = all.length - 1;
        if (n < 0) return;
        e.preventDefault(); all[n].focus(); all[n].click();
      });
    });
  }

  /* ---------- side decor: quiet icons in the margins of wide screens ----------
     One fixed layer (aria-hidden, no pointer events, compositor-only drift), built once after load.
     Motifs: window.PPK_DECOR (array of image URLs) if the page sets it; else the page's own brand icon
     (.ppk-brand img); else the icons of all programs (catalog, about). Off with data-ppk-decor="off";
     the piano game page (data-ppk-slash="off") keeps its own note background. */
  var DECOR_QUERY = '(min-width: 1280px)';
  function decorMotifs() {
    var own = window.PPK_DECOR;
    if (own && own.length) return Array.prototype.slice.call(own).filter(function (u) { return typeof u === 'string' && u; });
    var brand = doc.querySelector('.ppk-brand img');
    if (brand && (brand.currentSrc || brand.src)) return [brand.currentSrc || brand.src];
    var urls = [];
    for (var i = 0; i < programs.length; i++) if (programs[i].icon) urls.push(programs[i].icon);
    return urls;
  }
  function buildDecor() {
    var motifs = decorMotifs();
    if (!motifs.length) return;
    var perSide = motifs.length > 1 ? 8 : 6;
    var layer = el('div', 'ppk-decor'); layer.setAttribute('aria-hidden', 'true');
    var xs = [0.55, 0.15, 0.8, 0.35, 0.65, 0.1, 0.5, 0.85], rs = [-11, 7, -5, 12, -8, 4, -13, 9];
    var k = 0;
    ['l', 'r'].forEach(function (side, s) {
      var col = el('div', 'ppk-decor-side ppk-decor-' + side);
      for (var i = 0; i < perSide; i++, k++) {
        var img = el('img', 'ppk-decor-i');
        var j = (i + s * 3) % xs.length;
        img.alt = ''; img.decoding = 'async'; img.draggable = false;
        img.style.setProperty('--ppk-x', xs[j]);
        img.style.setProperty('--ppk-y', ((i + 0.2 + 0.5 * ((i * 5 + s * 3) % 4) / 4) / perSide * 100).toFixed(1) + '%');
        img.style.setProperty('--ppk-r', rs[(i * 3 + s * 5) % rs.length] + 'deg');
        img.style.setProperty('--ppk-k', (0.72 + 0.28 * (((i * 7 + s * 2) % 5) / 4)).toFixed(2));
        img.onerror = function () { if (this.parentNode) this.parentNode.removeChild(this); };
        img.src = local(motifs[(k * 3) % motifs.length]);
        col.appendChild(img);
      }
      layer.appendChild(col);
    });
    doc.body.appendChild(layer);
    requestAnimationFrame(function () { layer.classList.add('ppk-decor-on'); });
  }
  function initDecor() {
    var b = doc.body;
    if (!window.matchMedia || !b || b.getAttribute('data-ppk-decor') === 'off' || b.getAttribute('data-ppk-slash') === 'off') return;
    var mq = matchMedia(DECOR_QUERY), built = false;
    function start() {
      if (built || !mq.matches) return;
      built = true;
      var go = function () { if ('requestIdleCallback' in window) requestIdleCallback(buildDecor, { timeout: 2500 }); else setTimeout(buildDecor, 400); };
      if (doc.readyState === 'complete') go(); else window.addEventListener('load', go);
    }
    if (mq.addEventListener) mq.addEventListener('change', start); else if (mq.addListener) mq.addListener(start);
    start();
  }
  function init() {
    root.classList.add('ppk-js');
    initTheme(); initNav(); rewriteLinks(); initPalette(); initTerminals(); initReveal(); initTabs(); initDecor();
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init); else init();
})();

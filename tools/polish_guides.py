"""Idempotent shared-kit chrome (header, theme, footer) and responsive polish for existing HTML guides."""
from pathlib import Path
from html.parser import HTMLParser
import hashlib
import html
import json
import re

from build_presentation import ROOT, TEXT, nav_html, footer_html, head_assets, kit_version, load, e

HEAD_START = '<!-- ppk-head:start -->'
HEAD_END = '<!-- ppk-head:end -->'
BODY_START = '<!-- ppk-nav:start -->'
BODY_END = '<!-- ppk-nav:end -->'

CSS = '''/* Guide pages: the shared kit supplies tokens, header and footer; this file maps the older guide palette onto them. */
:root{--ink:var(--ppk-ink);--muted:var(--ppk-muted);--line:var(--ppk-line);--blue:var(--ppk-accent);--panel:var(--ppk-panel);--canvas:var(--ppk-bg)}
body.ppk-page{overflow-wrap:anywhere}
.ppk-page .shell a{color:var(--ppk-accent)}
.ppk-page .shell a:hover{color:var(--ppk-ink)}
.ppk-page .shell a.button,.ppk-page .shell a.button:hover{color:#0c315e}
.ppk-page .shell a.release-link{border-color:var(--ppk-line);background:var(--ppk-tint);color:var(--ppk-accent)}
.ppk-page .shell a.release-link:hover{color:var(--ppk-ink)}
.ppk-page .topic th{background:var(--ppk-tint)}
.ppk-page .topic code{background:var(--ppk-tint)}
.ppk-page .topic pre code{background:none}
.ppk-page .topic blockquote{border-left-color:var(--ppk-accent)}
.ppk-page figure{border-color:var(--ppk-line);background:var(--ppk-panel)}
.ppk-page .overview,.ppk-page .topic{box-shadow:none}
.ppk-page .guide-note{margin:24px 0 0;color:var(--ppk-muted);font-size:14px}
.ppk-page .shell img{max-width:100%;height:auto}
.ppk-page .shell pre{max-width:100%;overflow:auto}
.ppk-page .shell table{display:block;max-width:100%;overflow-x:auto}
.ppk-page .shell h1,.ppk-page .shell h2,.ppk-page .shell h3,.ppk-page .shell p,.ppk-page .shell li{overflow-wrap:anywhere}
.guide-tools{box-sizing:border-box;margin:0 0 16px;padding:6px 18px;border:1px solid var(--ppk-line);border-radius:14px;background:var(--ppk-panel);color:var(--ppk-ink);font:14px/1.6 var(--ppk-font)}
.guide-tools summary{cursor:pointer;min-height:44px;display:list-item;line-height:44px;font-weight:650}
.guide-tools ol{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 24px;margin:4px 0 12px;padding-left:22px}
.guide-tools li{min-width:0;overflow-wrap:anywhere}
.guide-full-image{display:block;max-width:100%;text-align:center}.guide-full-image img{object-fit:contain}
.ppk-page a:focus-visible,.ppk-page button:focus-visible,.ppk-page summary:focus-visible{outline:3px solid var(--ppk-focus);outline-offset:4px}
/* The guides' own `nav` rules must not restyle the kit header and footer. */
.ppk-nav-wrap .ppk-brand,.ppk-nav-wrap .ppk-back{padding:0;border:0;border-radius:0;background:none}
.ppk-nav-wrap .ppk-langs a{padding:0;border:0;background:transparent}
.ppk-nav-wrap .ppk-langs a:hover{background:var(--ppk-bg)}
.ppk-nav-wrap .ppk-langs a[aria-current]{background:var(--ppk-tint)}
.ppk-nav-wrap .ppk-nav{flex-wrap:nowrap}
.ppk-footer nav a{padding:0;border:0;border-radius:0;background:none;font-size:inherit}
@media(max-width:760px){.guide-tools ol{grid-template-columns:1fr}.ppk-page .shell{width:min(100% - 32px,1000px)}body .hero figure{display:block!important}}
@media(max-width:520px){.ppk-nav-wrap .ppk-nav{flex-wrap:wrap}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto!important}*,*:before,*:after{animation:none!important;transition:none!important}}
'''


HOME_START = '<!-- ppk-guide-home:start -->'
HOME_END = '<!-- ppk-guide-home:end -->'
HOME_LABEL = {'ru': '← На страницу программы', 'de': '← Zur Programmseite', 'en': '← Program page'}
HOME_PAGE = {'de': 'index.html', 'en': 'index-en.html', 'ru': 'index-ru.html'}
HOME_CSS = '''.guide-home{margin:0 0 20px}
body.ppk-page a.guide-home-link{display:inline-flex;align-items:center;min-height:44px;padding:10px 20px;border:1px solid var(--ppk-accent);border-radius:999px;background:var(--ppk-tint);color:var(--ppk-accent);font-size:15px;font-weight:700;line-height:1.3;text-decoration:none}
body.ppk-page a.guide-home-link:hover{background:var(--ppk-accent);color:var(--ppk-accent-ink,#fff)}
'''


def home_link(s, lang):
    """Visible link from the top of the guide to the landing page of this program in the same language."""
    s = re.sub(re.escape(HOME_START) + '.*?' + re.escape(HOME_END) + r'\s*', '', s, flags=re.S)
    block = HOME_START + '<p class="guide-home"><a class="guide-home-link" href="' + HOME_PAGE[lang] + '">' + HOME_LABEL[lang] + '</a></p>' + HOME_END + '\n'
    return re.sub(r'(<main\b[^>]*>)\s*', lambda m: m.group(1) + '\n' + block, s, count=1)


def css_rev(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()[:8]

CSS += HOME_CSS


def full_images(source, label):
    class Images(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.stack = []
            self.edits = []
            self.lines = source.splitlines(keepends=True)

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'img':
                src = attrs.get('src', '')
                classes = attrs.get('class', '') + ' ' + ' '.join(c for _, c in self.stack)
                if (not any(t == 'a' for t, _ in self.stack)
                        and not re.search(r'(screen-stack|screen-dark|screen-light|preview-dark|preview-light|popup-preview|app-frame)', classes)
                        and re.search(r'(screenshot|guide-(ru|de|en)|interface|demo-|app-(ru|de|en)|main-window|Ui-|control-|recordings-|surface-laptop|filters-)', src, re.I)):
                    line, col = self.getpos()
                    start = sum(map(len, self.lines[:line - 1])) + col
                    tagtext = self.get_starttag_text()
                    replacement = '<a class="guide-full-image" href="' + html.escape(src, quote=True) + '" aria-label="' + label + '">' + tagtext + '</a>'
                    self.edits.append((start, start + len(tagtext), replacement))
            if tag not in ('img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'wbr', 'area', 'base', 'embed', 'param'):
                self.stack.append((tag, attrs.get('class', '')))

        def handle_endtag(self, tag):
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    self.stack = self.stack[:i]
                    break

    p = Images()
    p.feed(source)
    for a, b, text in reversed(p.edits):
        source = source[:a] + text + source[b:]
    return source


def polish_pages():
    cfg, d = load()
    site = ROOT / cfg['folder']
    v = kit_version(cfg)
    paths = list(ROOT.glob('Guide-*.html')) + list((ROOT / 'docs').glob('Guide-*.html')) + list((ROOT / 'website').glob('Guide-*.html'))
    for path in paths:
        s = path.read_text(encoding='utf-8')
        lang = path.stem[-2:]
        if lang not in TEXT:
            continue
        t = TEXT[lang]
        s = re.sub(r'<title>.*?</title>', lambda _: '<title>%s</title>' % e(f'{d["name"]} — {t["guide"]}'), s, count=1, flags=re.S)
        # Keep release-summary generator output in sync when removing self links.
        start = s.find('<!-- public-release:start -->')
        end = s.find('<!-- public-release:end -->')
        if start >= 0 and end > start:
            part = s[start:end]
            part = re.sub(r'<a\b[^>]*href="[^"]*Guide-' + lang + r'\.html"[^>]*>.*?</a>', '', part, flags=re.S)
            part = re.sub(r'(<h2) id="section-\d+"', r'\1', part)
            s = s[:start] + part + s[end:]
        # Remove everything earlier runs (and the older guide-polish) added.
        s = re.sub(r'<!-- guide-polish:start -->.*?<!-- guide-polish:end -->\s*', '', s, flags=re.S)
        s = re.sub(r'<link rel="stylesheet" href="guide-polish.css(?:\?[^"]*)?">', '', s)
        s = re.sub(re.escape(HEAD_START) + '.*?' + re.escape(HEAD_END), '', s, flags=re.S)
        s = re.sub(re.escape(BODY_START) + '.*?' + re.escape(BODY_END) + r'\s*', '', s, flags=re.S)
        s = re.sub(r'<footer class="ppk-footer">.*?</footer>\s*', '', s, flags=re.S)
        s = re.sub(r'<meta name="color-scheme"[^>]*>', '', s)
        # The guide's own header is replaced by the kit header.
        s = re.sub(r'<header class="top">.*?</header>\s*', '', s, flags=re.S)
        # The old footer text stays as a short note inside the page.
        s = re.sub(r'<footer>(.*?)</footer>', r'<div class="guide-note">\1</div>', s, count=1, flags=re.S)
        # Table of contents from the section headings.
        headings = []
        release_start = s.find('<!-- public-release:start -->')
        release_end = s.find('<!-- public-release:end -->')

        def heading(m):
            if release_start >= 0 and release_start < m.start() < release_end:
                return m[0]
            attrs, title = m.group(1), m.group(2)
            label = html.unescape(re.sub('<[^>]+>', '', title)).strip()
            if not label:
                return m[0]
            existing = re.search(r'\bid="([^"]+)"', attrs)
            key = existing[1] if existing else 'section-' + str(len(headings) + 1)
            if not existing:
                attrs += ' id="' + key + '"'
            headings.append((key, label))
            return '<h2' + attrs + '>' + title + '</h2>'

        s = re.sub(r'<h2\b([^>]*)>(.*?)</h2>', heading, s, flags=re.S)
        s = re.sub(r'<aside class="guide-tools">.*?</aside>\s*', '', s, flags=re.S)
        has_contents = bool(re.search(r'class="[^"]*(?:toc|guide-nav|contents)[^"]*"', s))
        toc = ''
        if len(headings) > 3 and not has_contents:
            toc = ('<aside class="guide-tools"><details><summary>' + t['toc'] + '</summary><ol>'
                   + ''.join('<li><a href="#' + html.escape(k) + '">' + html.escape(lab) + '</a></li>' for k, lab in headings)
                   + '</ol></details></aside>\n')
        # Head: theme script first, kit styles, guide mapping.
        icon = cfg['icon']
        head = (HEAD_START + '<meta name="color-scheme" content="light dark">'
                + ('' if re.search(r'<link rel="icon"', s) else f'<link rel="icon" href="{e(icon)}">')
                + ('' if re.search(r'<meta name="description"', s) else f'<meta name="description" content="{e(d["description"][lang])}">')
                + head_assets(cfg) + f'<link rel="stylesheet" href="guide-kit.css?v={v}-{css_rev(CSS)}">' + HEAD_END)
        s = s.replace('</head>', head + '</head>', 1)
        # Body: class, skip link, kit header.
        nav = (BODY_START + f'<a class="ppk-skip" href="#main-content">{e(t["skip"])}</a>\n' + nav_html(cfg, d, lang, 'guide') + BODY_END + '\n')
        s = re.sub(r'<body\b([^>]*)>\s*', lambda m: _body(m, nav), s, count=1)
        s = re.sub(r'<main(?![^>]*\bid=)', '<main id="main-content"', s, count=1)
        s = home_link(s, lang)
        if toc:
            s = s.replace('<main', toc + '<main', 1)
        # Kit footer after the page container.
        s = re.sub(r'</div>\s*</body>', lambda m: '</div>\n' + footer_html(d, lang) + '\n</body>', s, count=1)
        s = full_images(s, t['full'])
        path.write_text(s, encoding='utf-8', newline='\n')
        (path.parent / 'guide-kit.css').write_text(CSS, encoding='utf-8', newline='\n')
        old = path.parent / 'guide-polish.css'
        if old.exists():
            old.unlink()


def _body(m, nav):
    attrs = m.group(1)
    if 'class=' in attrs:
        attrs = re.sub(r'class="([^"]*)"', lambda c: 'class="' + ' '.join(dict.fromkeys((c.group(1) + ' ppk-page ppk-prose').split())) + '"', attrs)
    else:
        attrs += ' class="ppk-page ppk-prose"'
    return '<body' + attrs + '>\n' + nav


def main():
    polish_pages()
    import site_images
    cfg, d = load()
    site_images.optimize(ROOT / cfg['folder'], d['repo'], ROOT, 'Guide-*.html')


if __name__ == '__main__':
    main()

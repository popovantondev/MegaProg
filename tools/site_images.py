"""Lighter program pages: local, right-sized images instead of raw.githubusercontent.com and 3000 px screenshots.

Run at the end of the page generators (idempotent). For every top-level *.html of the Pages root it:
  - turns raw.githubusercontent.com/popovantondev/<repo>/main/<path> image links into local relative paths
    (a file outside the Pages root is copied to assets/web/);
  - shows big screenshots (PNG/JPG wider than 1200 px or heavier than 100 KB) as WebP copies of at most 1100 px
    (assets/web/<name>.<hash>.webp); links around the image keep pointing at the original;
  - gives every <img> width/height, the first content image eager (the landing hero also fetchpriority=high),
    the rest loading=lazy decoding=async.
Originals stay where they are (the README links to them).
"""
from pathlib import Path
import hashlib
import re
import shutil

from PIL import Image

MAX_WIDTH = 1100
MAX_ICON = 256
BIG_WIDTH = 1200
BIG_BYTES = 100 * 1024
RASTER = ('.png', '.jpg', '.jpeg')
IMAGE_EXT = RASTER + ('.webp', '.svg', '.gif', '.ico')
RAW = re.compile(r'https://raw\.githubusercontent\.com/popovantondev/([^/"\'\s]+)/main/([^"\'\s?#<>]+)')
IMG = re.compile(r'<img\b[^>]*>')


def web_dir(root):
    """assets/web, spelled like an existing assets folder (some repositories track "Assets" and GitHub Pages is case-sensitive)."""
    for child in Path(root).iterdir():
        if child.is_dir() and child.name.lower() == 'assets':
            return child.name + '/web'
    return 'assets/web'


def get_attr(tag, name):
    m = re.search(r'\s' + name + r'(?:="([^"]*)")?(?=[\s/>])', tag)
    return None if not m else (m.group(1) if m.group(1) is not None else '')


def set_attr(tag, name, value):
    new = ' ' + name + '="' + value + '"'
    if get_attr(tag, name) is not None:
        return re.sub(r'\s' + name + r'(?:="[^"]*")?(?=[\s/>])', lambda _: new, tag, count=1)
    return re.sub(r'\s*(/?)>$', lambda m: new + (' /' if m.group(1) else '') + '>', tag, count=1)


def svg_size(path):
    head = path.read_text(encoding='utf-8', errors='replace')[:2000]
    m = re.search(r'<svg\b[^>]*>', head)
    if not m:
        return None
    tag = m.group(0)
    w, h = re.search(r'\swidth="([\d.]+)', tag), re.search(r'\sheight="([\d.]+)', tag)
    if w and h:
        return round(float(w.group(1))), round(float(h.group(1)))
    vb = re.search(r'viewBox="[\d.\-]+[ ,]+[\d.\-]+[ ,]+([\d.]+)[ ,]+([\d.]+)"', tag)
    return (round(float(vb.group(1))), round(float(vb.group(2)))) if vb else None


def image_size(path):
    if path.suffix.lower() == '.svg':
        return svg_size(path)
    with Image.open(path) as im:
        return im.size


def web_copy(src, root):
    """WebP copy of a big raster file: (path, (w, h) of the ORIGINAL), or None when the original is fine as it is.

    The width/height attributes keep the original proportions and size hint, so the layout stays exactly what it was
    (table columns and shrink-to-fit boxes are sized from them); only the downloaded file gets smaller."""
    if src.suffix.lower() not in RASTER:
        return None
    data = src.read_bytes()
    with Image.open(src) as im:
        w, h = im.size
        icon = 'icon' in src.stem.lower()
        if not (w > BIG_WIDTH or len(data) > BIG_BYTES or (icon and w > MAX_ICON)):
            return None
        limit = MAX_ICON if icon else MAX_WIDTH
        out = root / web_dir(root) / (src.stem + '.' + hashlib.md5(data).hexdigest()[:8] + '.webp')
        nw, nh = (w, h) if w <= limit else (limit, max(1, round(h * limit / w)))
        if not out.exists():
            im.load()
            frame = im.convert('RGBA' if ('A' in im.getbands() or im.mode == 'P') else 'RGB')
            if (nw, nh) != (w, h):
                frame = frame.resize((nw, nh), Image.LANCZOS)
            out.parent.mkdir(parents=True, exist_ok=True)
            frame.save(out, 'WEBP', quality=90 if icon else 84, method=6)
        if out.stat().st_size > len(data) * 0.85:
            out.unlink()
            return None
    return out, (w, h)


def local_from_raw(url_path, root, repo_root):
    """Local file for a raw.githubusercontent path of this repository, inside the Pages root if possible."""
    f = repo_root / url_path
    if not f.is_file():
        return None
    try:
        f.relative_to(root)
        return f
    except ValueError:
        target = root / web_dir(root) / f.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != f.read_bytes():
            shutil.copyfile(f, target)
        return target


def optimize_page(text, root, repo, repo_root, name):
    def raw_sub(m):
        if m.group(1) != repo or not m.group(2).lower().endswith(IMAGE_EXT):
            return m.group(0)
        f = local_from_raw(m.group(2), root, repo_root)
        return f.relative_to(root).as_posix() if f else m.group(0)

    text = RAW.sub(raw_sub, text)
    state = {'first': True}
    is_landing = name.startswith('index')

    def img_sub(m):
        tag = m.group(0)
        src = get_attr(tag, 'src')
        if not src or src.startswith(('http', 'data:')):
            return tag
        brand = 'ppk-brand' in text[max(0, m.start() - 160):m.start()]
        path = (root / src.split('?')[0].split('#')[0])
        if not path.is_file():
            return tag
        web = web_dir(root)
        if src.lower().startswith(web.lower() + '/') and not src.startswith(web + '/'):
            src = web + src[len(web):]
            tag = set_attr(tag, 'src', src)
        size = None
        converted = None if brand else web_copy(path, root)
        if converted:
            path, size = converted
            tag = set_attr(tag, 'src', path.relative_to(root).as_posix())
        size = size or image_size(path)
        if size:
            w = get_attr(tag, 'width')
            if w is not None and not re.fullmatch(r'\d+', w):
                tag = set_attr(tag, 'style', ((get_attr(tag, 'style') or '').rstrip(';') + ';' if get_attr(tag, 'style') else '') + 'width:' + w + ';height:auto')
                w = None
                tag = re.sub(r'\swidth="[^"]*"', '', tag, count=1)
            if w is None:
                tag = set_attr(tag, 'width', str(size[0]))
                tag = set_attr(tag, 'height', str(size[1]))
            elif get_attr(tag, 'height') is None:
                tag = set_attr(tag, 'height', str(round(int(w) * size[1] / size[0])))
        if not brand:
            if state['first']:
                if is_landing and get_attr(tag, 'fetchpriority') is None:
                    tag = set_attr(tag, 'fetchpriority', 'high')
            elif get_attr(tag, 'loading') is None and not re.search(r'dark|light', get_attr(tag, 'class') or ''):
                tag = set_attr(tag, 'loading', 'lazy')
            state['first'] = False
            if get_attr(tag, 'decoding') is None:
                tag = set_attr(tag, 'decoding', 'async')
        return tag

    return IMG.sub(img_sub, text)


def optimize(root, repo, repo_root=None, pattern='*.html'):
    """Optimise the top-level pages of a Pages root that match the glob; returns the list of changed files."""
    root = Path(root)
    repo_root = Path(repo_root) if repo_root else root
    changed = []
    for page in sorted(root.glob(pattern)):
        text = page.read_text(encoding='utf-8')
        new = optimize_page(text, root, repo, repo_root, page.name)
        if new != text:
            page.write_text(new, encoding='utf-8', newline='\n')
            changed.append(page.name)
    return changed

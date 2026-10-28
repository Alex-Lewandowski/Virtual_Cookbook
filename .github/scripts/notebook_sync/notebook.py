"""Reading and editing notebook content: Markdown references, links and the Source note."""

import json
import posixpath
import re
from urllib.parse import quote, unquote


MD_IMAGE = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*"|\s+\'[^\']*\')?\s*\)')


HTML_SRC = re.compile(r'<(?:img|source|video)\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)


MYST_IMAGE = re.compile(r'^[ \t>]*(?:`{3,}|:{3,})\s*\{(?:figure|image)\}\s+(\S+)', re.M)


MD_LINK = re.compile(r'(?<!!)\[([^\]]*)\]\(\s*(<?)([^)\s>]+)(>?)((?:\s+"[^"]*"|\s+\'[^\']*\')?)\s*\)')


HTML_HREF = re.compile(r'(<a\b[^>]*?\bhref\s*=\s*["\'])([^"\']+)(["\'])', re.I)


ATTRIBUTION_KEY = 'cookbook_sync'


def is_local(target):
    return not (
        re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target)  # http:, https:, mailto:, data:, ...
        or target.startswith(('#', '/', '//', '{'))
    )


def resolve(nb_path, target):
    """Resolve a relative reference against the notebook's folder upstream."""
    target = unquote(re.split(r'[?#]', target, maxsplit=1)[0])
    if not target:
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(nb_path), target))
    if resolved == '.' or resolved.startswith('../'):
        return None  # points outside the repository
    return resolved


def cell_text(cell):
    src = cell.get('source', '')
    return ''.join(src) if isinstance(src, list) else src


def set_cell_text(cell, text):
    cell['source'] = text.splitlines(keepends=True)


def point_images_at(nb, urls):
    """Replace image references (target -> URL) in Markdown cells."""
    if not urls:
        return

    def sub(m):
        target = m.group(1)
        if target not in urls:
            return m.group(0)
        whole, start = m.group(0), m.start(0)
        return whole[: m.start(1) - start] + urls[target] + whole[m.end(1) - start :]

    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'markdown':
            continue
        text = cell_text(cell)
        new = text
        for pattern in (MD_IMAGE, HTML_SRC, MYST_IMAGE):
            new = pattern.sub(sub, new)
        if new != text:
            set_cell_text(cell, new)


def rewrite_links(nb, src, upstream, copied):
    """Point links to upstream files that weren't copied at their GitHub pages."""

    def github_url(target):
        if not is_local(target):
            return None
        resolved = resolve(src.path, target)
        if resolved is None or resolved in copied:
            return None
        kind = upstream.kind(resolved)
        if kind is None:
            return None  # e.g. a MyST cross-reference label, not a file
        frag = '#' + target.split('#', 1)[1] if '#' in target else ''
        url = src.blob_url(resolved) if kind == 'blob' else src.tree_url(resolved)
        return url + frag

    def md_sub(m):
        url = github_url(m.group(3))
        if url is None:
            return m.group(0)
        return f'[{m.group(1)}]({m.group(2)}{url}{m.group(4)}{m.group(5)})'

    def href_sub(m):
        url = github_url(m.group(2))
        return m.group(0) if url is None else f'{m.group(1)}{url}{m.group(3)}'

    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'markdown':
            continue
        text = cell_text(cell)
        new = HTML_HREF.sub(href_sub, MD_LINK.sub(md_sub, text))
        if new != text:
            set_cell_text(cell, new)


def attribution_cell(nb, src, upstream, commit, date):
    lic_name, lic_path = upstream.license()
    repo_url = f'https://github.com/{src.slug}'
    commit_url = f'{repo_url}/blob/{commit}/{quote(src.path)}'
    if lic_name:
        license_text = f' and is shared under the [{lic_name} license]({src.blob_url(lic_path)})'
    elif lic_path:
        license_text = f' and is shared under [its license]({src.blob_url(lic_path)})'
    else:
        license_text = ''
    text = (
        ':::{note} Source\n'
        f'This notebook comes from [`{src.path}`]({src.blob_url()}) in the '
        f'[{src.slug}]({repo_url}) repository{license_text}. '
        'Credit for its content goes to the authors of that repository.\n'
        '\n'
        'This copy is updated automatically whenever the original changes, so please '
        f'suggest edits [upstream]({repo_url}/issues) rather than here. '
        f'It was last updated upstream in commit [`{commit[:7]}`]({commit_url}) on {date}.\n'
        ':::'
    )
    cell = {
        'cell_type': 'markdown',
        'metadata': {
            'tags': ['attribution'],
            ATTRIBUTION_KEY: {'source': src.blob_url(), 'commit': commit},
        },
        'source': text.splitlines(keepends=True),
    }
    if (nb.get('nbformat'), nb.get('nbformat_minor', 0)) >= (4, 5):
        cell['id'] = 'cookbook-sync-attribution'
    return cell


def insert_attribution(nb, cell):
    cells = nb.setdefault('cells', [])
    cells[:] = [c for c in cells if ATTRIBUTION_KEY not in c.get('metadata', {})]
    if not cells or cells[0].get('cell_type') != 'markdown':
        cells.insert(0, cell)
        return

    # Put the note directly under the notebook's title. The title stays first so
    # MyST still uses it as the page title; if the first cell continues past the
    # title, it is split so the note isn't pushed below the whole introduction.
    first = cells[0]
    m = re.match(r'(\s*(?:---\n.*?\n---[ \t]*\n)?\s*(?:#[ \t]+[^\n]*(?:\n|$))?)(.*)', cell_text(first), re.S)
    head, rest = m.group(1), m.group(2).lstrip('\n')
    if not head.strip():
        cells.insert(0, cell)  # no title or frontmatter to keep above the note
        return
    cells.insert(1, cell)
    if rest.strip():
        set_cell_text(first, head.rstrip('\n'))
        body = json.loads(json.dumps(first))
        set_cell_text(body, rest)
        if 'id' in body:
            body['id'] = (body['id'][:58] + '-intro')
        cells.insert(2, body)


def dump_notebook(nb):
    return (json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + '\n').encode()

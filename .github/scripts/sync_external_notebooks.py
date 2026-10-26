#!/usr/bin/env python3
"""Sync notebooks from other GitHub repositories into this Cookbook.

Reads cookbook_sources.yml at the repository root and, for every listed notebook:

* copies the latest upstream version to external/<owner>/<repo>/<original path>,
  mirroring the upstream layout so relative paths inside the notebook keep working;
* copies the images the notebook's Markdown cells reference (Markdown images, HTML
  <img> tags and MyST {figure}/{image} directives), creating folders as needed;
* rewrites links to other upstream files that were not copied so they point at
  GitHub instead of breaking;
* inserts a Markdown cell that credits the original notebook, its repository,
  license and the upstream commit it was copied from;
* lists the notebook in myst.yml inside a block delimited by marker comments.

The run is deterministic: if nothing changed upstream, nothing changes here.
Files that a removed source used to bring in are deleted.

Usage: python .github/scripts/sync_external_notebooks.py
Requires git and PyYAML.
"""

import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'cookbook_sources.yml'
MYST = ROOT / 'myst.yml'
DEST_DIR = 'external'
LOCK = ROOT / DEST_DIR / 'sources.lock.json'

TOC_BEGIN = '# >>> external notebooks: managed by .github/scripts/sync_external_notebooks.py, do not edit by hand'
TOC_END = '# <<< external notebooks'
ATTRIBUTION_KEY = 'cookbook_sync'
DEFAULT_SECTION = 'Community Notebooks'

GITHUB_URL = re.compile(
    r'^https?://github\.com/([^/]+)/([^/]+)/(?:blob|tree|raw)/([^/]+)/(.+)$'
)

# References to files inside Markdown cells.
MD_IMAGE = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*"|\s+\'[^\']*\')?\s*\)')
HTML_SRC = re.compile(r'<(?:img|source|video)\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)
MYST_IMAGE = re.compile(r'^[ \t>]*(?:`{3,}|:{3,})\s*\{(?:figure|image)\}\s+(\S+)', re.M)
MD_LINK = re.compile(r'(?<!!)\[([^\]]*)\]\(\s*(<?)([^)\s>]+)(>?)((?:\s+"[^"]*"|\s+\'[^\']*\')?)\s*\)')
HTML_HREF = re.compile(r'(<a\b[^>]*?\bhref\s*=\s*["\'])([^"\']+)(["\'])', re.I)


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # Shows up as a warning annotation on the workflow run.
    print(f'::warning::{msg}', flush=True)


def error(msg):
    print(f'::error::{msg}', flush=True)


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    owner: str
    repo: str
    ref: str
    path: str
    title: str = None
    group: str = None
    include: list = field(default_factory=list)

    @property
    def slug(self):
        return f'{self.owner}/{self.repo}'

    @property
    def dest(self):
        return posixpath.join(DEST_DIR, self.owner, self.repo, self.path)

    def dest_for(self, upstream_path):
        return posixpath.join(DEST_DIR, self.owner, self.repo, upstream_path)

    def blob_url(self, path=None, ref=None):
        return f'https://github.com/{self.slug}/blob/{quote(ref or self.ref)}/{quote(path or self.path)}'

    def tree_url(self, path, ref=None):
        return f'https://github.com/{self.slug}/tree/{quote(ref or self.ref)}/{quote(path)}'


def load_manifest():
    data = yaml.safe_load(MANIFEST.read_text()) or {}
    section = data.get('toc_section') or DEFAULT_SECTION
    sources = []
    for i, entry in enumerate(data.get('sources') or [], start=1):
        if isinstance(entry, str):
            entry = {'url': entry}
        try:
            if entry.get('url'):
                url = entry['url'].split('#')[0].split('?')[0]
                m = GITHUB_URL.match(url)
                if not m:
                    raise ValueError(f'not a GitHub file URL: {entry["url"]}')
                owner, repo, ref, path = m.groups()
            else:
                owner, repo = entry['repo'].split('/')
                ref, path = entry.get('ref', 'main'), entry['path']
            repo = repo.removesuffix('.git')
            src = Source(
                owner=owner,
                repo=repo,
                ref=str(entry.get('ref') or ref),
                path=unquote(path).strip('/'),
                title=entry.get('title'),
                group=entry.get('group'),
                include=[str(p).strip('/') for p in entry.get('include') or []],
            )
        except (KeyError, ValueError) as exc:
            raise SystemExit(f'cookbook_sources.yml entry {i} is invalid: {exc}')
        if not src.path.endswith('.ipynb'):
            raise SystemExit(f'cookbook_sources.yml entry {i} is not a notebook: {src.path}')
        sources.append(src)
    dests = [s.dest for s in sources]
    dupes = {d for d in dests if dests.count(d) > 1}
    if dupes:
        raise SystemExit(f'cookbook_sources.yml lists the same notebook twice: {sorted(dupes)}')
    return section, sources


# --------------------------------------------------------------------------- #
# Upstream repositories (partial clones: history and trees, blobs on demand)
# --------------------------------------------------------------------------- #


class Upstream:
    def __init__(self, owner, repo, ref, workdir):
        self.dir = Path(workdir) / f'{owner}__{repo}__{re.sub(r"[^A-Za-z0-9._-]", "_", ref)}'
        self.dir.mkdir(parents=True)
        self.git('init', '-q')
        self.git('remote', 'add', 'origin', f'https://github.com/{owner}/{repo}.git')
        self.git('fetch', '-q', '--filter=blob:none', '--no-tags', 'origin', ref)
        self.head = self.git('rev-parse', 'FETCH_HEAD').strip()

    def git(self, *args, binary=False, check=True):
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        res = subprocess.run(
            ['git', '-C', str(self.dir), *args], capture_output=True, env=env
        )
        if check and res.returncode != 0:
            raise RuntimeError(f'git {" ".join(args)} failed: {res.stderr.decode().strip()}')
        return res.stdout if binary else res.stdout.decode()

    def kind(self, path):
        res = self.git('cat-file', '-t', f'{self.head}:{path}', check=False)
        return res.strip() or None  # 'blob', 'tree' or None

    def read(self, path):
        return self.git('cat-file', 'blob', f'{self.head}:{path}', binary=True)

    def files_under(self, path):
        out = self.git('ls-tree', '-r', '--name-only', self.head, '--', path)
        return [p for p in out.splitlines() if p]

    def last_change(self, path):
        sha, date = self.git('log', '-1', '--format=%H %cs', self.head, '--', path).split()
        return sha, date

    def license(self):
        """Return (name, path) of the repository license, best effort."""
        names = self.git('ls-tree', '--name-only', self.head).splitlines()
        candidates = [n for n in names if re.match(r'(?i)^(licen[cs]e|copying)(\.(md|txt|rst))?$', n)]
        if not candidates:
            return None, None
        path = sorted(candidates)[0]
        text = self.read(path).decode('utf-8', 'replace')
        flat = ' '.join(text.split()).lower()
        checks = [
            ('apache license' in flat and 'version 2.0' in flat, 'Apache-2.0'),
            ('permission is hereby granted, free of charge' in flat, 'MIT'),
            ('gnu lesser general public license' in flat, 'LGPL'),
            ('gnu affero general public license' in flat, 'AGPL-3.0'),
            ('gnu general public license' in flat and 'version 3' in flat, 'GPL-3.0'),
            ('gnu general public license' in flat and 'version 2' in flat, 'GPL-2.0'),
            ('mozilla public license' in flat, 'MPL-2.0'),
            ('creative commons attribution 4.0' in flat or 'cc-by-4.0' in flat, 'CC-BY-4.0'),
            (
                'redistribution and use in source and binary forms' in flat
                and 'neither the name' in flat,
                'BSD-3-Clause',
            ),
            ('redistribution and use in source and binary forms' in flat, 'BSD-2-Clause'),
            ('unlicense' in flat and 'public domain' in flat, 'Unlicense'),
        ]
        name = next((n for ok, n in checks if ok), None)
        return name, path


# --------------------------------------------------------------------------- #
# Notebook processing
# --------------------------------------------------------------------------- #


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


def find_images(nb, nb_path):
    found = set()
    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'markdown':
            continue
        text = cell_text(cell)
        for pattern in (MD_IMAGE, HTML_SRC, MYST_IMAGE):
            for target in pattern.findall(text):
                if is_local(target):
                    resolved = resolve(nb_path, target)
                    if resolved is None:
                        warn(f'{nb_path}: skipping image outside the repository: {target}')
                    else:
                        found.add((target, resolved))
    return found


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
    # Keep a leading title cell (or frontmatter) first so MyST still uses it as the page title.
    index = 0
    if cells and cells[0].get('cell_type') == 'markdown':
        first = cell_text(cells[0]).lstrip()
        if first.startswith('# ') or first.startswith('---'):
            index = 1
    cells.insert(index, cell)


def dump_notebook(nb):
    return (json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + '\n').encode()


# --------------------------------------------------------------------------- #
# myst.yml table of contents
# --------------------------------------------------------------------------- #


def toc_block(section, entries, indent):
    """entries: list of (group, title, file) in manifest order."""
    pad = ' ' * indent
    q = json.dumps  # a JSON string is a valid YAML double-quoted string
    lines = [f'{pad}{TOC_BEGIN}']
    if entries:
        lines += [f'{pad}- title: {q(section)}', f'{pad}  children:']

        def item(title, file, depth):
            p = pad + '  ' * depth
            out = [f'{p}- file: {file}']
            if title:
                out.append(f'{p}  title: {q(title)}')
            return out

        groups = []
        for group, _, _ in entries:
            if group not in groups:
                groups.append(group)
        for group in groups:
            members = [(t, f) for g, t, f in entries if g == group]
            if group is None:
                for title, file in members:
                    lines += item(title, file, 2)
            else:
                lines += [f'{pad}    - title: {q(group)}', f'{pad}      children:']
                for title, file in members:
                    lines += item(title, file, 4)
    lines.append(f'{pad}{TOC_END}')
    return lines


def update_myst(section, entries):
    lines = MYST.read_text().splitlines()
    begin = next((i for i, l in enumerate(lines) if l.strip() == TOC_BEGIN), None)
    end = next((i for i, l in enumerate(lines) if l.strip() == TOC_END), None)

    if begin is not None and end is not None and begin < end:
        indent = len(lines[begin]) - len(lines[begin].lstrip())
        lines[begin : end + 1] = toc_block(section, entries, indent)
    else:
        toc = next((i for i, l in enumerate(lines) if re.match(r'^\s+toc:\s*$', l)), None)
        if toc is None:
            raise SystemExit('myst.yml has no project.toc list to add notebooks to')
        key_indent = len(lines[toc]) - len(lines[toc].lstrip())
        item_indent, last = None, toc
        for i in range(toc + 1, len(lines)):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith('#'):
                continue
            indent = len(lines[i]) - len(lines[i].lstrip())
            if indent <= key_indent and not stripped.startswith('- '):
                break
            if item_indent is None and stripped.startswith('- '):
                item_indent = indent
            last = i
        if item_indent is None:
            item_indent = key_indent + 2
        lines[last + 1 : last + 1] = toc_block(section, entries, item_indent)

    text = '\n'.join(lines) + '\n'
    yaml.safe_load(text)  # fail loudly rather than write a broken myst.yml
    MYST.write_text(text)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def write_file(rel, data, written):
    if written.get(rel) is not None and written[rel] != data:
        raise RuntimeError(f'two sources want to write different content to {rel}')
    written[rel] = data
    path = ROOT / rel
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    log(f'  updated {rel}')


def sync_source(src, upstream, written):
    if upstream.kind(src.path) != 'blob':
        raise RuntimeError(f'{src.path} not found in {src.slug}@{src.ref}')
    commit, date = upstream.last_change(src.path)
    nb = json.loads(upstream.read(src.path))

    files = set()
    for inc in src.include:
        kind = upstream.kind(inc)
        if kind is None:
            warn(f'{src.slug}: include path not found: {inc}')
        for path in [inc] if kind == 'blob' else upstream.files_under(inc) if kind else []:
            write_file(src.dest_for(path), upstream.read(path), written)
            files.add(path)

    for target, resolved in sorted(find_images(nb, src.path)):
        if upstream.kind(resolved) != 'blob':
            warn(f'{src.slug}/{src.path}: image not found upstream: {target}')
            continue
        write_file(src.dest_for(resolved), upstream.read(resolved), written)
        files.add(resolved)

    rewrite_links(nb, src, upstream, files | {src.path})
    insert_attribution(nb, attribution_cell(nb, src, upstream, commit, date))
    write_file(src.dest, dump_notebook(nb), written)

    return {
        'source': src.blob_url(),
        'commit': commit,
        'files': sorted([src.dest] + [src.dest_for(f) for f in files]),
    }


def main():
    section, sources = load_manifest()
    old_lock = json.loads(LOCK.read_text()) if LOCK.exists() else {'notebooks': {}}
    old = old_lock.get('notebooks', {})

    new, written, failures = {}, {}, []
    upstreams = {}
    with tempfile.TemporaryDirectory() as tmp:
        for src in sources:
            log(f'{src.slug}: {src.path} ({src.ref})')
            try:
                key = (src.owner, src.repo, src.ref)
                if key not in upstreams:
                    upstreams[key] = Upstream(*key, tmp)
                new[src.dest] = sync_source(src, upstreams[key], written)
            except Exception as exc:  # keep the last good copy and carry on
                error(f'{src.slug}/{src.path}: {exc}')
                failures.append(src)
                if src.dest in old:
                    new[src.dest] = old[src.dest]
                    written.update({f: None for f in old[src.dest]['files']})

    # Delete files that only removed or changed sources brought in.
    keep = {f for entry in new.values() for f in entry['files']}
    stale = {f for entry in old.values() for f in entry['files']} - keep
    for rel in sorted(stale):
        path = ROOT / rel
        if path.exists():
            path.unlink()
            log(f'  removed {rel}')
        parent = path.parent
        while parent != ROOT / DEST_DIR and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    entries = [
        (src.group, src.title, src.dest)
        for src in sources
        if src.dest in new
    ]
    update_myst(section, entries)

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps({'notebooks': new}, indent=2, sort_keys=True) + '\n')

    log(f'Synced {len(sources) - len(failures)} of {len(sources)} notebooks.')
    if failures:
        sys.exit(1)


if __name__ == '__main__':
    main()

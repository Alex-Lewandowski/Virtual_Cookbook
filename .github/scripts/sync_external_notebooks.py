#!/usr/bin/env python3
"""Sync notebooks from other GitHub repositories into this Cookbook.

Reads cookbook_sources.yml at the repository root and, for every listed notebook:

* copies the latest upstream version to external/<owner>/<repo>/<original path>,
  mirroring the upstream layout so relative paths inside the notebook keep working;
* copies the images the notebook's Markdown cells reference (Markdown images, HTML
  <img> tags and MyST {figure}/{image} directives), creating folders as needed;
* copies the local Python modules the notebook imports (or %runs), and the modules
  those import in turn, at the same relative paths so the imports keep working;
* copies files in the notebook's repository that its code (or those modules) names
  by a literal path, such as "data/aoi.geojson", up to 10 MB each;
* copies anything listed under the notebook's `include:` key: paths in its own
  repository, files or folders from other GitHub repositories, or any URL;
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

import ast
import fnmatch
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

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
RAW_GITHUB_URL = re.compile(r'^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$')
LFS_POINTER = b'version https://git-lfs.github.com/spec/v1'

# Files a notebook's code mentions are copied automatically only up to this size;
# list bigger ones under `include:` if you really want them in this repository.
AUTO_MAX_BYTES = 10 * 1024 * 1024
# GitHub refuses pushes containing files over 100 MB.
MAX_BYTES = 100 * 1024 * 1024

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
class Extra:
    """An `include:` entry: a path in the notebook's repo, or a URL, plus where it goes."""

    path: str = None  # file or folder in the notebook's own repository
    url: str = None  # GitHub file/folder URL in any repository, or any http(s) URL
    to: str = None  # destination, relative to the notebook's repository copy

    @property
    def github(self):
        """(owner, repo, ref, path) for a GitHub URL, else None."""
        if not self.url:
            return None
        url = self.url.split('#')[0].split('?')[0]
        m = GITHUB_URL.match(url) or RAW_GITHUB_URL.match(url)
        if not m:
            return None
        owner, repo, ref, path = m.groups()
        return owner, repo.removesuffix('.git'), ref, unquote(path).strip('/')


def safe_relpath(value, what):
    path = posixpath.normpath(str(value).strip().strip('/'))
    if not value or str(value).startswith('/') or path == '.' or path.startswith('..'):
        raise ValueError(f'{what} must be a relative path inside the repository: {value!r}')
    return path


def parse_include(item):
    if isinstance(item, str):
        if re.match(r'^https?://', item):
            return Extra(url=item)
        return Extra(path=safe_relpath(item, 'include path'))
    if isinstance(item, dict) and (item.get('url') or item.get('path')):
        unknown = set(item) - {'url', 'path', 'to'}
        if unknown:
            raise ValueError(f'unknown key(s) in include entry: {sorted(unknown)}')
        if item.get('url') and item.get('path'):
            raise ValueError('an include entry takes either url or path, not both')
        to = safe_relpath(item['to'], 'include "to"') if item.get('to') else None
        if item.get('path'):
            return Extra(path=safe_relpath(item['path'], 'include path'), to=to)
        if not re.match(r'^https?://', str(item['url'])):
            raise ValueError(f'include url must start with http:// or https://: {item["url"]}')
        return Extra(url=str(item['url']), to=to)
    raise ValueError(f'include entries must be a path, a URL, or a mapping with url/path and to: {item!r}')


@dataclass
class Source:
    owner: str
    repo: str
    ref: str
    path: str
    title: str = None
    group: str = None
    include: list = field(default_factory=list)
    exclude: list = field(default_factory=list)

    def excluded(self, path):
        """True if a path in the notebook's repository matches an `exclude:` pattern."""
        for pattern in self.exclude:
            folder = pattern.rstrip('/')
            if fnmatch.fnmatchcase(path, pattern) or path == folder or path.startswith(folder + '/'):
                return True
        return False

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
                include=[parse_include(item) for item in entry.get('include') or []],
                exclude=[safe_relpath(p, 'exclude pattern') for p in entry.get('exclude') or []],
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
        self.slug = f'{owner}/{repo}'
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

    def tree(self):
        """Every file path in the repository, and every folder (cached)."""
        if not hasattr(self, '_tree'):
            files = set(self.files_under('.'))
            dirs = {''}
            for f in files:
                parts = f.split('/')[:-1]
                dirs.update('/'.join(parts[: i + 1]) for i in range(len(parts)))
            self._tree = files, dirs
        return self._tree

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


RUN_MAGIC = re.compile(r'^\s*%run\s+(?:-\S+\s+)*["\']?([^\s"\']+\.py)\b', re.M)
PYTHON_CELL_MAGICS = {'time', 'timeit', 'capture', 'prun', 'debug'}


def python_source(code):
    """Turn notebook cell code into parseable Python by blanking IPython syntax."""
    lines = code.splitlines()
    if lines and lines[0].lstrip().startswith('%%'):
        magic = lines[0].lstrip()[2:].split()[0] if lines[0].strip() != '%%' else ''
        if magic not in PYTHON_CELL_MAGICS:
            return ''  # %%bash, %%writefile, ... are not Python
        lines = lines[1:]
    out = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        # Shell escapes, line magics, help (`x?`) and `x = !cmd` become no-ops.
        if (
            stripped.startswith(('!', '%', '?'))
            or re.match(r'^[\w.,\s]+=\s*[!%]', stripped)
            or (stripped.endswith('?') and not stripped.startswith('#'))
        ):
            out.append(f'{indent}pass')
        else:
            out.append(line)
    return '\n'.join(out)


def parse_python(code):
    """Parse Python code into AST nodes, falling back to line by line on a syntax error."""
    flags = ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

    def parse(text):
        try:
            return [compile(text, '<cell>', 'exec', flags=flags, dont_inherit=True)]
        except (SyntaxError, ValueError):
            return []

    trees = parse(code)
    if not trees:
        # One odd line shouldn't hide everything else in the cell.
        trees = [t for line in code.splitlines() for t in parse(line.strip())]
    return [n for tree in trees for n in ast.walk(tree)]


def imports_in(code):
    """Yield (level, module, names) for each import statement in Python code."""
    for node in parse_python(code):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name, []
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or '', [a.name for a in node.names if a.name != '*']


def ancestors(folder):
    """The folder itself, then each parent up to the repository root ('')."""
    out = [folder]
    while folder:
        folder = posixpath.dirname(folder)
        out.append(folder)
    return out


def module_files(dotted, base, files, dirs):
    """Files that importing `dotted` from folder `base` loads, or None if it isn't there."""
    parts = [p for p in dotted.split('.') if p]
    if not parts:
        return None
    found, path = [], base
    for i, part in enumerate(parts):
        path = posixpath.join(path, part)
        if path + '.py' in files:
            return found + [path + '.py'] if i == len(parts) - 1 else None
        if path not in dirs:
            return None
        init = posixpath.join(path, '__init__.py')
        if init in files:
            found.append(init)
        elif not any(f.startswith(path + '/') and f.endswith('.py') for f in files):
            return None  # a plain folder, e.g. data/, not a package
    return found


def find_local_code(nb, nb_path, upstream, skip=lambda path: False):
    """Python files in the notebook's repository that the notebook imports or %runs.

    Modules are looked up the way they'd be found when the notebook runs from its
    own folder, including the common `sys.path.insert(0, parent)` pattern: the
    notebook's folder first, then each parent up to the repository root. Imports
    inside the copied files are followed too, so helpers of helpers come along.
    Files for which `skip(path)` is true are left out, along with their imports.
    """
    files, dirs = upstream.tree()
    nb_bases = ancestors(posixpath.dirname(nb_path))
    found, queue = set(), []

    def add(paths):
        for p in paths:
            if p not in found and not skip(p):
                found.add(p)
                if p.endswith('.py'):
                    queue.append(p)

    def resolve_imports(code, package_dir, bases):
        for level, module, names in imports_in(code):
            if level:
                if package_dir is None:
                    continue  # relative import in a notebook: not resolvable
                base = package_dir
                for _ in range(level - 1):
                    base = posixpath.dirname(base)
                candidates = [base]
            else:
                candidates = bases
            for base in candidates:
                hit = module_files(module, base, files, dirs) if module else []
                if hit is None:
                    continue
                add(hit)
                # `from pkg import mod` loads pkg/mod.py when mod is a submodule.
                for name in names:
                    sub = f'{module}.{name}' if module else name
                    extra = module_files(sub, base, files, dirs)
                    if extra:
                        add(extra)
                break  # Python stops at the first folder that has the module

    for cell in nb.get('cells', []):
        if cell.get('cell_type') != 'code':
            continue
        code = cell_text(cell)
        for target in RUN_MAGIC.findall(code):
            resolved = resolve(nb_path, target)
            if resolved in files:
                add([resolved])
        resolve_imports(python_source(code), None, nb_bases)

    while queue:
        path = queue.pop()
        file_dir = posixpath.dirname(path)
        code = upstream.read(path).decode('utf-8', 'replace')
        # A helper's own folder is searched first, so `import sibling` in a script works.
        bases = [file_dir] + [b for b in nb_bases if b != file_dir]
        resolve_imports(code, file_dir, bases)

    return sorted(found)


def path_strings(code):
    """String literals in Python code that could be relative file paths."""
    for node in parse_python(code):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            if (
                value
                and len(value) < 300
                and '\n' not in value
                and ('.' in value or '/' in value)
                and is_local(value)
                and not re.search(r'[*?{}\[\]<>|]', value)  # globs, templates, regexes
            ):
                yield value


def find_data_files(nb, nb_path, code_files, upstream):
    """Files in the notebook's repository that its code refers to by a literal path.

    Looks at the string literals in the notebook's code cells and in the local
    modules copied with it. A string counts only if it names an existing file:
    relative to the notebook's folder (where it runs) or one of its parents, or,
    inside a module, relative to that module's folder (the `Path(__file__).parent
    / "x.csv"` pattern). Folders, notebooks and Git LFS files are left out.
    """
    files, _ = upstream.tree()
    nb_bases = ancestors(posixpath.dirname(nb_path))
    sources = [(python_source(cell_text(c)), nb_bases) for c in nb.get('cells', []) if c.get('cell_type') == 'code']
    for path in code_files:
        if path.endswith('.py'):
            here = posixpath.dirname(path)
            code = upstream.read(path).decode('utf-8', 'replace')
            sources.append((code, [here] + [b for b in nb_bases if b != here]))

    found = set()
    for code, bases in sources:
        for value in path_strings(code):
            for base in bases:
                resolved = posixpath.normpath(posixpath.join(base, value))
                if resolved in files and not resolved.endswith('.ipynb'):
                    found.add(resolved)
                    break
    return sorted(found)


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


TOC_FILE = re.compile(r'^\s*(?:-\s+)?file:\s*["\']?([^"\'#\s]+)')


def update_myst(section, entries):
    """Rebuild the managed TOC block; return external files listed elsewhere that no longer exist.

    Notebooks you've placed yourself elsewhere in myst.yml (outside the marker
    comments) stay where you put them and are left out of the managed block.
    """
    lines = MYST.read_text().splitlines()
    begin = next((i for i, l in enumerate(lines) if l.strip() == TOC_BEGIN), None)
    end = next((i for i, l in enumerate(lines) if l.strip() == TOC_END), None)

    has_block = begin is not None and end is not None and begin < end
    placed = {
        m.group(1)
        for i, l in enumerate(lines)
        if not (has_block and begin <= i <= end)
        and (m := TOC_FILE.match(l))
        and m.group(1).startswith(DEST_DIR + '/')
    }
    synced = {file for _, _, file in entries}
    for file in sorted(placed & synced):
        log(f'  {file} is placed elsewhere in myst.yml; leaving it there')
    entries = [e for e in entries if e[2] not in placed]
    stale = sorted(f for f in placed - synced if not (ROOT / f).exists())

    if has_block:
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
    return stale


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


def download(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'cookbook-notebook-sync'})
    with urllib.request.urlopen(req, timeout=120) as res:
        data = res.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError(f'{url} is over 100 MB, which GitHub does not accept')
    return data


def read_blob(upstream, path):
    """A file's content, following Git LFS pointers to the real file."""
    data = upstream.read(path)
    if data.startswith(LFS_POINTER):
        data = download(
            f'https://media.githubusercontent.com/media/{upstream.slug}/{upstream.head}/{quote(path)}'
        )
    if len(data) > MAX_BYTES:
        raise RuntimeError(f'{upstream.slug}/{path} is over 100 MB, which GitHub does not accept')
    return data


def copy_from_repo(upstream, path, to, copy, where):
    """Copy a file or folder from a repository; `to` renames or relocates it."""
    kind = upstream.kind(path)
    if kind is None:
        warn(f'{where}: not found in {upstream.slug}: {path}')
        return
    for f in [path] if kind == 'blob' else upstream.files_under(path):
        if to is None:
            dest = f
        elif kind == 'blob':
            dest = to
        else:
            dest = posixpath.join(to, posixpath.relpath(f, path))
        copy(dest, read_blob(upstream, f))


def sync_source(src, upstream, written, get_upstream):
    if upstream.kind(src.path) != 'blob':
        raise RuntimeError(f'{src.path} not found in {src.slug}@{src.ref}')
    commit, date = upstream.last_change(src.path)
    nb = json.loads(upstream.read(src.path))
    where = f'{src.slug}/{src.path}'

    # Paths of everything copied, relative to this notebook's repository copy.
    files = set()

    def copy(dest, data):
        write_file(src.dest_for(dest), data, written)
        files.add(dest)

    # 1. Files listed under `include:` in cookbook_sources.yml, copied whatever their size.
    for inc in src.include:
        if inc.path:
            copy_from_repo(upstream, inc.path, inc.to, copy, where)
        elif inc.github:
            owner, repo, ref, path = inc.github
            other = upstream if (owner, repo, ref) == (src.owner, src.repo, src.ref) else get_upstream(owner, repo, ref)
            copy_from_repo(other, path, inc.to, copy, where)
        else:
            name = posixpath.basename(unquote(urlparse(inc.url).path)) or 'download'
            copy(inc.to or posixpath.join(posixpath.dirname(src.path), name), download(inc.url))

    # Steps 2-4 find files automatically; `exclude:` patterns override them,
    # but anything listed under `include:` (already copied above) is kept.

    # 2. Images used in Markdown cells. Excluded ones are shown from GitHub instead.
    remote_images = {}
    for target, resolved in sorted(find_images(nb, src.path)):
        if resolved in files:
            continue
        if upstream.kind(resolved) != 'blob':
            warn(f'{where}: image not found upstream: {target}')
        elif src.excluded(resolved):
            remote_images[target] = (
                f'https://raw.githubusercontent.com/{src.slug}/{quote(src.ref)}/{quote(resolved)}'
            )
        else:
            copy(resolved, read_blob(upstream, resolved))
    point_images_at(nb, remote_images)

    # 3. Local Python modules the notebook imports.
    code_files = find_local_code(nb, src.path, upstream, skip=src.excluded)
    for path in code_files:
        if path not in files:
            copy(path, read_blob(upstream, path))

    # 4. Files the notebook's (or its modules') code names by a literal path.
    for path in find_data_files(nb, src.path, code_files, upstream):
        if path in files or src.excluded(path):
            continue
        data = upstream.read(path)
        if data.startswith(LFS_POINTER) or len(data) > AUTO_MAX_BYTES:
            warn(
                f'{where}: not copying {path} automatically because it is over 10 MB or stored with Git LFS; '
                'list it under include: in cookbook_sources.yml to copy it anyway'
            )
            continue
        copy(path, data)

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

        def get_upstream(owner, repo, ref):
            key = (owner.lower(), repo.lower(), ref)
            if key not in upstreams:
                upstreams[key] = Upstream(owner, repo, ref, tmp)
            return upstreams[key]

        for src in sources:
            log(f'{src.slug}: {src.path} ({src.ref})')
            try:
                upstream = get_upstream(src.owner, src.repo, src.ref)
                new[src.dest] = sync_source(src, upstream, written, get_upstream)
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
    stale = update_myst(section, entries)
    for file in stale:
        error(
            f'myst.yml lists {file}, but that notebook is no longer synced. '
            'Remove it from myst.yml, or add its source back to cookbook_sources.yml.'
        )

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps({'notebooks': new}, indent=2, sort_keys=True) + '\n')

    log(f'Synced {len(sources) - len(failures)} of {len(sources)} notebooks.')
    if failures or stale:
        sys.exit(1)


if __name__ == '__main__':
    main()

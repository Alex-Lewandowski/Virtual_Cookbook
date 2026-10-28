"""Shared test helpers: an in-memory stand-in for upstream repositories.

The tests never touch the network. `FakeUpstream` answers the same questions
the real `Upstream` does (does this path exist, what's in this file, ...) from
a dict of {path: content}.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeUpstream:
    def __init__(self, slug, files, head='0123456789abcdef0123456789abcdef01234567'):
        self.slug = slug
        self.head = head
        self.files = {p: c if isinstance(c, bytes) else c.encode() for p, c in files.items()}

    def kind(self, path):
        path = path.rstrip('/')
        if path in self.files:
            return 'blob'
        return 'tree' if any(f.startswith(path + '/') for f in self.files) else None

    def read(self, path):
        return self.files[path]

    def files_under(self, path):
        path = path.rstrip('/')
        if path in ('', '.'):
            return sorted(self.files)
        return sorted(f for f in self.files if f == path or f.startswith(path + '/'))

    def tree(self):
        dirs = {''}
        for f in self.files:
            parts = f.split('/')[:-1]
            dirs.update('/'.join(parts[: i + 1]) for i in range(len(parts)))
        return set(self.files), dirs

    def last_change(self, path):
        return self.head, '2026-10-28'

    def license(self):
        return ('MIT', 'LICENSE') if 'LICENSE' in self.files else (None, None)


def notebook(*cells, minor=5):
    """Build a notebook from ('markdown' | 'code', source) pairs."""
    out = []
    for i, (kind, source) in enumerate(cells):
        cell = {'cell_type': kind, 'metadata': {}, 'source': source}
        if minor >= 5:
            cell['id'] = f'cell-{i}'
        if kind == 'code':
            cell.update(outputs=[], execution_count=None)
        out.append(cell)
    return {'nbformat': 4, 'nbformat_minor': minor, 'metadata': {}, 'cells': out}


MYST_YML = """\
version: 1
project:
  title: Test Cookbook
  toc:
    - file: README.md
    - title: Setup
      children:
        - file: notebooks/setup.ipynb

site:
  template: book-theme
"""


@pytest.fixture
def cookbook(tmp_path):
    """A minimal Cookbook repository with a myst.yml and README.md."""
    (tmp_path / 'myst.yml').write_text(MYST_YML)
    (tmp_path / 'README.md').write_text('# Test\n')
    return tmp_path


@pytest.fixture
def write_manifest(cookbook):
    """Write cookbook_sources.yml in the `cookbook` repository."""

    def write(sources, section='Community Notebooks'):
        (cookbook / 'cookbook_sources.yml').write_text(
            yaml.safe_dump({'toc_section': section, 'sources': sources}, sort_keys=False)
        )

    return write


@pytest.fixture
def upstreams():
    """A registry of fake repositories, plus a factory to pass to sync.run()."""
    repos = {}

    def make_upstream(owner, repo, ref, workdir):
        try:
            return repos[(owner, repo)]
        except KeyError:
            raise RuntimeError(f'git fetch failed: {owner}/{repo} not found') from None

    def add(slug, files):
        owner, repo = slug.split('/')
        repos[(owner, repo)] = FakeUpstream(slug, files)
        return repos[(owner, repo)]

    make_upstream.add = add
    make_upstream.repos = repos
    return make_upstream


def read_notebook(path):
    return json.loads(Path(path).read_text())


def text_of(cell):
    src = cell['source']
    return ''.join(src) if isinstance(src, list) else src

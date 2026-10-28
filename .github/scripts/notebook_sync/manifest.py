"""Reading cookbook_sources.yml: which notebooks to sync, and their options."""

import fnmatch
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

import yaml

from .common import DEFAULT_SECTION, DEST_DIR


GITHUB_URL = re.compile(
    r'^https?://github\.com/([^/]+)/([^/]+)/(?:blob|tree|raw)/([^/]+)/(.+)$'
)


RAW_GITHUB_URL = re.compile(r'^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$')



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


def load_manifest(path):
    """Return (toc_section, [Source, ...]) from cookbook_sources.yml at `path`."""
    data = yaml.safe_load(path.read_text()) or {}
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

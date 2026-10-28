"""Reading files from upstream GitHub repositories and the web.

Repositories are fetched as partial clones of a single branch: commit history
and folder listings up front, file contents only when a file is read.
"""

import os
import re
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import quote


LFS_POINTER = b'version https://git-lfs.github.com/spec/v1'


MAX_BYTES = 100 * 1024 * 1024


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

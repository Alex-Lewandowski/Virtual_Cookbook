"""Running a sync: copying each source's files, cleaning up, and updating the TOC."""

import json
import posixpath
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .common import DEST_DIR, LOCK_FILE, MANIFEST_FILE, error, log, warn
from .detect import find_data_files, find_images, find_local_code
from .manifest import load_manifest
from .notebook import attribution_cell, dump_notebook, insert_attribution, point_images_at, rewrite_links
from .toc import update_myst
from .upstream import LFS_POINTER, Upstream, download, read_blob


# Files a notebook's code mentions are copied automatically only up to this size;
# list bigger ones under `include:` if you really want them in this repository.
AUTO_MAX_BYTES = 10 * 1024 * 1024


def write_file(root, rel, data, written):
    if written.get(rel) is not None and written[rel] != data:
        raise RuntimeError(f'two sources want to write different content to {rel}')
    written[rel] = data
    path = root / rel
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    log(f'  updated {rel}')


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


def sync_source(root, src, upstream, written, get_upstream):
    if upstream.kind(src.path) != 'blob':
        raise RuntimeError(f'{src.path} not found in {src.slug}@{src.ref}')
    commit, date = upstream.last_change(src.path)
    nb = json.loads(upstream.read(src.path))
    where = f'{src.slug}/{src.path}'

    # Paths of everything copied, relative to this notebook's repository copy.
    files = set()

    def copy(dest, data):
        write_file(root, src.dest_for(dest), data, written)
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
    write_file(root, src.dest, dump_notebook(nb), written)

    return {
        'source': src.blob_url(),
        'commit': commit,
        'files': sorted([src.dest] + [src.dest_for(f) for f in files]),
    }


def run(root, make_upstream=Upstream):
    """Sync every source listed in the Cookbook at `root`; return True if all succeeded.

    `make_upstream(owner, repo, ref, workdir)` opens an upstream repository; tests
    pass a stand-in so no network access is needed.
    """
    root = Path(root)
    section, sources = load_manifest(root / MANIFEST_FILE)
    lock = root / LOCK_FILE
    old_lock = json.loads(lock.read_text()) if lock.exists() else {'notebooks': {}}
    old = old_lock.get('notebooks', {})

    new, written, failures = {}, {}, []
    upstreams = {}
    with tempfile.TemporaryDirectory() as tmp:

        def get_upstream(owner, repo, ref):
            key = (owner.lower(), repo.lower(), ref)
            if key not in upstreams:
                upstreams[key] = make_upstream(owner, repo, ref, tmp)
            return upstreams[key]

        for src in sources:
            log(f'{src.slug}: {src.path} ({src.ref})')
            try:
                upstream = get_upstream(src.owner, src.repo, src.ref)
                new[src.dest] = sync_source(root, src, upstream, written, get_upstream)
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
        path = root / rel
        if path.exists():
            path.unlink()
            log(f'  removed {rel}')
        parent = path.parent
        while parent != root / DEST_DIR and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    entries = [
        (src.group, src.title, src.dest)
        for src in sources
        if src.dest in new
    ]
    stale = update_myst(root, section, entries)
    for file in stale:
        error(
            f'myst.yml lists {file}, but that notebook is no longer synced. '
            'Remove it from myst.yml, or add its source back to cookbook_sources.yml.'
        )

    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({'notebooks': new}, indent=2, sort_keys=True) + '\n')

    log(f'Synced {len(sources) - len(failures)} of {len(sources)} notebooks.')
    return not (failures or stale)


def main():
    # notebook_sync/sync.py -> notebook_sync -> scripts -> .github -> repository root
    root = Path(__file__).resolve().parents[3]
    sys.exit(0 if run(root) else 1)

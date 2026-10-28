"""End-to-end runs of the whole sync against fake upstream repositories."""

import json

from conftest import notebook, read_notebook, text_of

from notebook_sync import sync
from notebook_sync.notebook import ATTRIBUTION_KEY

NB_URL = 'https://github.com/Org/Repo/blob/main/nbs/demo.ipynb'
DEST = 'external/Org/Repo'

DEMO = notebook(
    ('markdown', '# Demo\n\nIntro.\n\n![logo](img/logo.png) ![anim](img/anim.gif)\n'),
    ('code', (
        'import sys\nsys.path.insert(0, "..")\nfrom util.io import load\n'
        'df = load("data/table.csv")\nbig = "data/big.tif"\nmask = "data/mask.npy"\n'
    )),
)

REPO = {
    'LICENSE': 'MIT',
    'nbs/demo.ipynb': json.dumps(DEMO),
    'nbs/img/logo.png': 'PNG',
    'nbs/img/anim.gif': 'GIF',
    'nbs/data/table.csv': 'a,b',
    'nbs/data/mask.npy': 'NPY',
    'nbs/data/big.tif': b'0' * (sync.AUTO_MAX_BYTES + 1),
    'util/__init__.py': '',
    'util/io.py': 'from .plot import show\n',
    'util/plot.py': '',
    'util/unused.py': '',
    'docs/extra/one.txt': '1',
}


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob('*'))
        if p.is_file()
    }


def test_full_sync(cookbook, write_manifest, upstreams, capsys):
    upstreams.add('Org/Repo', REPO)
    write_manifest([NB_URL])
    assert sync.run(cookbook, upstreams) is True

    files = sorted(p for p in snapshot(cookbook) if p.startswith('external/Org'))
    assert files == [
        f'{DEST}/nbs/data/mask.npy',
        f'{DEST}/nbs/data/table.csv',
        f'{DEST}/nbs/demo.ipynb',
        f'{DEST}/nbs/img/anim.gif',
        f'{DEST}/nbs/img/logo.png',
        f'{DEST}/util/__init__.py',
        f'{DEST}/util/io.py',
        f'{DEST}/util/plot.py',
    ]
    assert 'not copying nbs/data/big.tif automatically' in capsys.readouterr().out

    nb = read_notebook(cookbook / DEST / 'nbs/demo.ipynb')
    assert ATTRIBUTION_KEY in nb['cells'][1]['metadata']
    lock = json.loads((cookbook / 'external/sources.lock.json').read_text())
    assert sorted(lock['notebooks'][f'{DEST}/nbs/demo.ipynb']['files']) == files
    assert f'- file: {DEST}/nbs/demo.ipynb' in (cookbook / 'myst.yml').read_text()


def test_second_run_changes_nothing(cookbook, write_manifest, upstreams):
    upstreams.add('Org/Repo', REPO)
    write_manifest([NB_URL])
    sync.run(cookbook, upstreams)
    before = snapshot(cookbook)
    sync.run(cookbook, upstreams)
    assert snapshot(cookbook) == before


def test_removing_a_source_removes_its_files(cookbook, write_manifest, upstreams):
    upstreams.add('Org/Repo', REPO)
    write_manifest([NB_URL])
    sync.run(cookbook, upstreams)
    write_manifest([])
    assert sync.run(cookbook, upstreams) is True
    assert not (cookbook / 'external/Org').exists()
    assert DEST not in (cookbook / 'myst.yml').read_text()


def test_include_and_exclude(cookbook, write_manifest, upstreams, monkeypatch):
    upstreams.add('Org/Repo', REPO)
    upstreams.add('Other/Lib', {'lookup/a.csv': 'A', 'lookup/more/b.csv': 'B'})
    monkeypatch.setattr(sync, 'download', lambda url: f'downloaded {url}'.encode())
    write_manifest([{
        'url': NB_URL,
        'exclude': ['nbs/img/*.gif', 'util/plot.py', 'nbs/data/table.csv', 'nbs/data/mask.npy'],
        'include': [
            'docs/extra',
            'nbs/data/table.csv',  # include wins over exclude
            'nbs/data/big.tif',  # include ignores the automatic size limit
            {'url': 'https://github.com/Other/Lib/tree/main/lookup', 'to': 'nbs/lookup'},
            {'url': 'https://example.org/files/remote.csv'},
            {'url': 'https://example.org/files/remote.csv', 'to': 'nbs/data/renamed.csv'},
        ],
    }])
    assert sync.run(cookbook, upstreams) is True
    files = snapshot(cookbook)

    assert f'{DEST}/docs/extra/one.txt' in files
    assert f'{DEST}/nbs/data/table.csv' in files
    assert f'{DEST}/nbs/data/big.tif' in files
    assert files[f'{DEST}/nbs/lookup/a.csv'] == b'A'
    assert files[f'{DEST}/nbs/lookup/more/b.csv'] == b'B'
    assert files[f'{DEST}/nbs/remote.csv'] == b'downloaded https://example.org/files/remote.csv'
    assert f'{DEST}/nbs/data/renamed.csv' in files

    assert f'{DEST}/util/plot.py' not in files
    assert f'{DEST}/nbs/data/mask.npy' not in files
    assert f'{DEST}/nbs/img/anim.gif' not in files
    nb = read_notebook(cookbook / DEST / 'nbs/demo.ipynb')
    intro = text_of(nb['cells'][2])
    assert '![anim](https://raw.githubusercontent.com/Org/Repo/main/nbs/img/anim.gif)' in intro
    assert '![logo](img/logo.png)' in intro


def test_failed_source_keeps_last_good_copy(cookbook, write_manifest, upstreams):
    repo = upstreams.add('Org/Repo', REPO)
    write_manifest([NB_URL])
    sync.run(cookbook, upstreams)
    before = snapshot(cookbook)

    del repo.files['nbs/demo.ipynb']  # the notebook disappears upstream
    assert sync.run(cookbook, upstreams) is False
    assert snapshot(cookbook) == before


def test_one_failure_does_not_stop_the_others(cookbook, write_manifest, upstreams):
    upstreams.add('Org/Repo', REPO)
    write_manifest([
        'https://github.com/Missing/Repo/blob/main/x.ipynb',
        NB_URL,
    ])
    assert sync.run(cookbook, upstreams) is False
    assert (cookbook / DEST / 'nbs/demo.ipynb').exists()

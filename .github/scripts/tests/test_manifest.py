import pytest
import yaml

from notebook_sync.manifest import Extra, load_manifest, parse_include


def write(tmp_path, data):
    path = tmp_path / 'cookbook_sources.yml'
    path.write_text(yaml.safe_dump(data))
    return path


def test_reads_url_entries_with_options(tmp_path):
    path = write(tmp_path, {
        'toc_section': 'Imported',
        'sources': [
            'https://github.com/Org/Repo/blob/main/nbs/a.ipynb',
            {
                'url': 'https://github.com/Org/Other.git/blob/dev/b%20c.ipynb#top',
                'title': 'B', 'group': 'G', 'ref': 'v1.0',
            },
        ],
    })
    section, (a, b) = load_manifest(path)
    assert section == 'Imported'
    assert (a.owner, a.repo, a.ref, a.path) == ('Org', 'Repo', 'main', 'nbs/a.ipynb')
    assert a.dest == 'external/Org/Repo/nbs/a.ipynb'
    assert (b.repo, b.ref, b.path, b.title, b.group) == ('Other', 'v1.0', 'b c.ipynb', 'B', 'G')


def test_default_section(tmp_path):
    path = write(tmp_path, {'sources': []})
    assert load_manifest(path) == ('Community Notebooks', [])


@pytest.mark.parametrize('entry, message', [
    ({'url': 'https://example.org/a.ipynb'}, 'not a GitHub file URL'),
    ({'url': 'https://github.com/Org/Repo/blob/main/script.py'}, 'is not a notebook'),
    ({'url': 'https://github.com/Org/Repo/blob/main/a.ipynb', 'include': ['../x']}, 'relative path'),
    ({'url': 'https://github.com/Org/Repo/blob/main/a.ipynb', 'exclude': ['/etc']}, 'relative path'),
])
def test_rejects_invalid_entries(tmp_path, entry, message):
    with pytest.raises(SystemExit, match=message):
        load_manifest(write(tmp_path, {'sources': [entry]}))


def test_rejects_duplicate_notebooks(tmp_path):
    url = 'https://github.com/Org/Repo/blob/main/a.ipynb'
    with pytest.raises(SystemExit, match='same notebook twice'):
        load_manifest(write(tmp_path, {'sources': [url, {'url': url}]}))


@pytest.mark.parametrize('item, expected', [
    ('data/sample.csv', Extra(path='data/sample.csv')),
    ('data/aoi/', Extra(path='data/aoi')),
    ('https://example.org/x.csv', Extra(url='https://example.org/x.csv')),
    ({'path': 'a.csv', 'to': 'b/a.csv'}, Extra(path='a.csv', to='b/a.csv')),
    ({'url': 'https://example.org/x.csv', 'to': 'data/x.csv'}, Extra(url='https://example.org/x.csv', to='data/x.csv')),
])
def test_include_forms(item, expected):
    assert parse_include(item) == expected


@pytest.mark.parametrize('item, message', [
    ('../outside', 'relative path'),
    ('/etc/passwd', 'relative path'),
    ({'url': 'ftp://example.org/x'}, 'http'),
    ({'url': 'https://a', 'path': 'b'}, 'not both'),
    ({'path': 'a', 'extra': 1}, 'unknown key'),
    ({'to': 'x'}, 'must be a path'),
    ({'path': 'a', 'to': '../b'}, 'relative path'),
])
def test_include_rejects(item, message):
    with pytest.raises(ValueError, match=message):
        parse_include(item)


@pytest.mark.parametrize('url, expected', [
    ('https://github.com/Org/Lib/blob/main/data/x.csv', ('Org', 'Lib', 'main', 'data/x.csv')),
    ('https://github.com/Org/Lib/tree/v2/data/', ('Org', 'Lib', 'v2', 'data')),
    ('https://raw.githubusercontent.com/Org/Lib/main/x%20y.csv', ('Org', 'Lib', 'main', 'x y.csv')),
    ('https://example.org/x.csv', None),
])
def test_include_github_urls(url, expected):
    assert Extra(url=url).github == expected


def test_exclude_matching(tmp_path):
    path = write(tmp_path, {'sources': [{
        'url': 'https://github.com/Org/Repo/blob/main/a.ipynb',
        'exclude': ['data/big.h5', 'assets/', 'img/*.gif'],
    }]})
    _, (src,) = load_manifest(path)
    assert src.excluded('data/big.h5')
    assert src.excluded('assets/logo.png')
    assert src.excluded('assets/deep/logo.png')
    assert src.excluded('img/anim.gif')
    assert not src.excluded('data/small.csv')
    assert not src.excluded('assets_old/logo.png')
    assert not src.excluded('img/still.png')

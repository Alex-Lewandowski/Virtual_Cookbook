from conftest import FakeUpstream, notebook, text_of

from notebook_sync.manifest import Source
from notebook_sync.notebook import (
    ATTRIBUTION_KEY,
    attribution_cell,
    insert_attribution,
    point_images_at,
    rewrite_links,
)

SRC = Source(owner='Org', repo='Repo', ref='main', path='nbs/demo.ipynb')


def note(nb):
    up = FakeUpstream('Org/Repo', {'nbs/demo.ipynb': '', 'LICENSE': ''})
    return attribution_cell(nb, SRC, up, 'abcdef1234567890', '2026-10-28')


def test_attribution_text():
    text = text_of(note(notebook()))
    assert text.startswith(':::{note} Source\n')
    assert '[`nbs/demo.ipynb`](https://github.com/Org/Repo/blob/main/nbs/demo.ipynb)' in text
    assert '[MIT license](https://github.com/Org/Repo/blob/main/LICENSE)' in text
    assert '[`abcdef1`](https://github.com/Org/Repo/blob/abcdef1234567890/nbs/demo.ipynb) on 2026-10-28' in text


def test_note_goes_under_the_title_and_splits_the_intro():
    nb = notebook(('markdown', '# Title\n\nIntro text.\n'), ('code', 'x = 1'))
    insert_attribution(nb, note(nb))
    cells = nb['cells']
    assert [text_of(c) for c in cells[:1]] == ['# Title']
    assert ATTRIBUTION_KEY in cells[1]['metadata']
    assert text_of(cells[2]) == 'Intro text.\n'
    assert cells[2]['id'] == 'cell-0-intro'
    assert len({c['id'] for c in cells}) == len(cells)


def test_note_goes_first_without_a_title():
    nb = notebook(('markdown', 'No heading here.'), minor=4)
    insert_attribution(nb, note(nb))
    assert ATTRIBUTION_KEY in nb['cells'][0]['metadata']
    assert 'id' not in nb['cells'][0]


def test_note_is_replaced_not_duplicated():
    nb = notebook(('markdown', '# Title'))
    insert_attribution(nb, note(nb))
    insert_attribution(nb, note(nb))
    assert sum(ATTRIBUTION_KEY in c['metadata'] for c in nb['cells']) == 1


def test_rewrite_links_points_uncopied_files_at_github():
    up = FakeUpstream('Org/Repo', {'nbs/demo.ipynb': '', 'nbs/other.ipynb': '', 'util/x.py': '', 'nbs/copied.md': ''})
    nb = notebook(('markdown', (
        '[other](other.ipynb#part) [folder](../util) [copied](copied.md) '
        '[label](some-label) <a href="../util/x.py">x</a> [web](https://example.org)'
    )))
    rewrite_links(nb, SRC, up, {'nbs/demo.ipynb', 'nbs/copied.md'})
    assert text_of(nb['cells'][0]) == (
        '[other](https://github.com/Org/Repo/blob/main/nbs/other.ipynb#part) '
        '[folder](https://github.com/Org/Repo/tree/main/util) [copied](copied.md) '
        '[label](some-label) <a href="https://github.com/Org/Repo/blob/main/util/x.py">x</a> '
        '[web](https://example.org)'
    )


def test_point_images_at():
    nb = notebook(('markdown', '![a](a.png) ![b](b.png)\n<img src="a.png">\n:::{figure} a.png\n:::'))
    point_images_at(nb, {'a.png': 'https://x/a.png'})
    assert text_of(nb['cells'][0]) == (
        '![a](https://x/a.png) ![b](b.png)\n<img src="https://x/a.png">\n:::{figure} https://x/a.png\n:::'
    )

import yaml

from notebook_sync.toc import TOC_BEGIN, TOC_END, update_myst

A = 'external/Org/Repo/a.ipynb'
B = 'external/Org/Repo/b.ipynb'


def toc(root):
    return yaml.safe_load((root / 'myst.yml').read_text())['project']['toc']


def test_adds_block_at_end_of_toc(cookbook):
    assert update_myst(cookbook, 'Imported', [(None, None, A), ('Group', 'Bee', B)]) == []
    assert toc(cookbook)[-1] == {'title': 'Imported', 'children': [
        {'file': A},
        {'title': 'Group', 'children': [{'file': B, 'title': 'Bee'}]},
    ]}
    text = (cookbook / 'myst.yml').read_text()
    assert text.count(TOC_BEGIN) == 1 and text.count(TOC_END) == 1
    assert yaml.safe_load(text)['site'] == {'template': 'book-theme'}


def test_updates_existing_block_in_place(cookbook):
    update_myst(cookbook, 'Imported', [(None, None, A), (None, None, B)])
    update_myst(cookbook, 'Imported', [(None, None, B)])
    text = (cookbook / 'myst.yml').read_text()
    assert text.count(TOC_BEGIN) == 1
    assert toc(cookbook)[-1] == {'title': 'Imported', 'children': [{'file': B}]}


def test_empty_block_when_nothing_synced(cookbook):
    update_myst(cookbook, 'Imported', [(None, None, A)])
    update_myst(cookbook, 'Imported', [])
    assert len(toc(cookbook)) == 2


def test_notebooks_placed_elsewhere_stay_put(cookbook):
    update_myst(cookbook, 'Imported', [(None, None, A), (None, None, B)])
    myst = cookbook / 'myst.yml'
    text = myst.read_text().replace(f'        - file: {A}\n', '', 1)
    text = text.replace('        - file: notebooks/setup.ipynb\n', f'        - file: notebooks/setup.ipynb\n        - file: {A}\n')
    myst.write_text(text)

    (cookbook / A).parent.mkdir(parents=True)
    (cookbook / A).write_text('{}')
    assert update_myst(cookbook, 'Imported', [(None, None, A), (None, None, B)]) == []
    assert myst.read_text().count(A) == 1
    assert {'file': A} in toc(cookbook)[1]['children']
    assert toc(cookbook)[-1] == {'title': 'Imported', 'children': [{'file': B}]}

    # Its source removed and its file deleted: reported, and myst.yml left valid.
    (cookbook / A).unlink()
    assert update_myst(cookbook, 'Imported', [(None, None, B)]) == [A]
    assert myst.read_text().count(A) == 1


def test_hand_placed_external_file_that_exists_is_not_reported(cookbook):
    manual = 'external/Manual/notes.md'
    (cookbook / manual).parent.mkdir(parents=True)
    (cookbook / manual).write_text('# Notes')
    myst = cookbook / 'myst.yml'
    myst.write_text(myst.read_text().replace('    - file: README.md\n', f'    - file: README.md\n    - file: {manual}\n'))
    assert update_myst(cookbook, 'Imported', [(None, None, A)]) == []
    assert myst.read_text().count(manual) == 1

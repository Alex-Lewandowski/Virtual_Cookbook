from conftest import FakeUpstream, notebook

from notebook_sync.detect import (
    find_data_files,
    find_images,
    find_local_code,
    imports_in,
    python_source,
)


def test_find_images():
    nb = notebook(('markdown', (
        '![a](img/a.png) ![b](<img/b.png> "title")\n'
        '<img src="../assets/c.svg" width=10>\n'
        ':::{figure} ../assets/d.png\n:::\n'
        '```{image} e.png\n```\n'
        '![web](https://example.org/x.png) ![outside](../../x.png) ![anchor](#x)\n'
    )), ('code', '# ![not markdown](z.png)'))
    found = find_images(nb, 'nbs/demo.ipynb')
    assert sorted(r for _, r in found) == [
        'assets/c.svg', 'assets/d.png', 'nbs/e.png', 'nbs/img/a.png', 'nbs/img/b.png',
    ]


def test_python_source_blanks_ipython_syntax():
    code = '%matplotlib inline\n!pip install x\nfiles = !ls\nobj?\nimport os\nif True:\n    %time f()\n'
    assert python_source(code) == 'pass\npass\npass\npass\nimport os\nif True:\n    pass'
    assert python_source('%%bash\nimport nope') == ''
    assert python_source('%%time\nimport yes') == 'import yes'


def test_imports_in_survives_syntax_errors_and_await():
    assert list(imports_in('import a.b\nfrom . import c\nawait x()')) == [
        (0, 'a.b', []), (1, '', ['c']),
    ]
    assert list(imports_in('def broken(:\nimport ok')) == [(0, 'ok', [])]


UPSTREAM_FILES = {
    'nbs/a/demo.ipynb': '',
    'util/__init__.py': '',
    'util/config.py': 'from . import util\nimport numpy\n',
    'util/util.py': 'from .deep import x\n',
    'util/deep.py': 'import helpers\n',
    'util/helpers.py': '',
    'util/unused.py': '',
    'nbs/a/local_mod.py': 'import json\n',
    'scripts/run_me.py': 'from tools.t import y\n',
    'tools/t.py': '',
    'nbs/a/sub/__init__.py': '',
    'nbs/a/sub/m.py': '',
    'data/table.csv': '',
}

NOTEBOOK = notebook(
    ('code', '%matplotlib inline\nimport sys\nsys.path.insert(0, "../..")\nfrom util import config\n'),
    ('code', 'import local_mod\nx = !ls\nobj?\n'),
    ('code', '%%bash\nimport nope\n'),
    ('code', '%%time\nfrom sub import m\n'),
    ('code', '%run ../../scripts/run_me.py\n'),
    ('code', 'import data\nawait foo()\nfrom util import *\n'),
)


def test_find_local_code_follows_imports():
    up = FakeUpstream('Org/Repo', UPSTREAM_FILES)
    assert find_local_code(NOTEBOOK, 'nbs/a/demo.ipynb', up) == [
        'nbs/a/local_mod.py',
        'nbs/a/sub/__init__.py',
        'nbs/a/sub/m.py',
        'scripts/run_me.py',
        'tools/t.py',
        'util/__init__.py',
        'util/config.py',
        'util/deep.py',
        'util/helpers.py',
        'util/util.py',
    ]


def test_find_local_code_skip_stops_at_excluded_module():
    up = FakeUpstream('Org/Repo', UPSTREAM_FILES)
    found = find_local_code(NOTEBOOK, 'nbs/a/demo.ipynb', up, skip=lambda p: p == 'util/util.py')
    assert 'util/util.py' not in found
    assert 'util/deep.py' not in found  # only util/util.py imported it
    assert 'util/config.py' in found


def test_find_local_code_uses_the_nearest_module_like_python():
    up = FakeUpstream('Org/Repo', {
        'nbs/demo.ipynb': '', 'nbs/helpers.py': '', 'helpers.py': '',
    })
    nb = notebook(('code', 'import helpers'))
    assert find_local_code(nb, 'nbs/demo.ipynb', up) == ['nbs/helpers.py']


def test_find_data_files():
    files = {
        'nbs/demo.ipynb': '',
        'nbs/data/table.csv': '', 'nbs/data/a.tif': '', 'nbs/other.ipynb': '',
        'shared/aoi.geojson': '',
        'nbs/helpers/__init__.py': '',
        'nbs/helpers/io.py': 'HERE = Path(__file__).parent\nLUT = HERE / "lut.json"\n',
        'nbs/helpers/lut.json': '',
        'README.md': '',
    }
    nb = notebook(('code', (
        'df = pd.read_csv("data/table.csv")\n'
        'aoi = open("../shared/aoi.geojson")\n'
        'tifs = glob("data/*.tif")\n'
        'folder = Path("data")\n'
        'other = "other.ipynb"\n'
        'url = "https://example.org/data/table.csv"\n'
        'missing = "data/missing.csv"\n'
        'readme = "README.md"\n'  # found from a parent folder
    )))
    up = FakeUpstream('Org/Repo', files)
    found = find_data_files(nb, 'nbs/demo.ipynb', ['nbs/helpers/io.py'], up)
    assert found == ['README.md', 'nbs/data/table.csv', 'nbs/helpers/lut.json', 'shared/aoi.geojson']

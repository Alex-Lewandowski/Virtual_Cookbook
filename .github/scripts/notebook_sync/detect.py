"""Finding the files a notebook needs: images, local Python modules and data files."""

import ast
import posixpath
import re

from .common import warn
from .notebook import HTML_SRC, MD_IMAGE, MYST_IMAGE, cell_text, is_local, resolve


RUN_MAGIC = re.compile(r'^\s*%run\s+(?:-\S+\s+)*["\']?([^\s"\']+\.py)\b', re.M)


PYTHON_CELL_MAGICS = {'time', 'timeit', 'capture', 'prun', 'debug'}


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

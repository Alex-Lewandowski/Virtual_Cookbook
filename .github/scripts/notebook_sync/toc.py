"""Keeping the synced notebooks listed in myst.yml's table of contents."""

import json
import re

import yaml

from .common import DEST_DIR, MYST_FILE, log


# These marker comments are already in myst.yml files; changing them would make the
# sync add a second block instead of updating the existing one.
TOC_BEGIN = '# >>> external notebooks: managed by .github/scripts/sync_external_notebooks.py, do not edit by hand'


TOC_END = '# <<< external notebooks'


TOC_FILE = re.compile(r'^\s*(?:-\s+)?file:\s*["\']?([^"\'#\s]+)')


def toc_block(section, entries, indent):
    """entries: list of (group, title, file) in manifest order."""
    pad = ' ' * indent
    q = json.dumps  # a JSON string is a valid YAML double-quoted string
    lines = [f'{pad}{TOC_BEGIN}']
    if entries:
        lines += [f'{pad}- title: {q(section)}', f'{pad}  children:']

        def item(title, file, depth):
            p = pad + '  ' * depth
            out = [f'{p}- file: {file}']
            if title:
                out.append(f'{p}  title: {q(title)}')
            return out

        groups = []
        for group, _, _ in entries:
            if group not in groups:
                groups.append(group)
        for group in groups:
            members = [(t, f) for g, t, f in entries if g == group]
            if group is None:
                for title, file in members:
                    lines += item(title, file, 2)
            else:
                lines += [f'{pad}    - title: {q(group)}', f'{pad}      children:']
                for title, file in members:
                    lines += item(title, file, 4)
    lines.append(f'{pad}{TOC_END}')
    return lines


def update_myst(root, section, entries):
    """Rebuild the managed TOC block; return external files listed elsewhere that no longer exist.

    Notebooks you've placed yourself elsewhere in myst.yml (outside the marker
    comments) stay where you put them and are left out of the managed block.
    """
    myst = root / MYST_FILE
    lines = myst.read_text().splitlines()
    begin = next((i for i, l in enumerate(lines) if l.strip() == TOC_BEGIN), None)
    end = next((i for i, l in enumerate(lines) if l.strip() == TOC_END), None)

    has_block = begin is not None and end is not None and begin < end
    placed = {
        m.group(1)
        for i, l in enumerate(lines)
        if not (has_block and begin <= i <= end)
        and (m := TOC_FILE.match(l))
        and m.group(1).startswith(DEST_DIR + '/')
    }
    synced = {file for _, _, file in entries}
    for file in sorted(placed & synced):
        log(f'  {file} is placed elsewhere in myst.yml; leaving it there')
    entries = [e for e in entries if e[2] not in placed]
    stale = sorted(f for f in placed - synced if not (root / f).exists())

    if has_block:
        indent = len(lines[begin]) - len(lines[begin].lstrip())
        lines[begin : end + 1] = toc_block(section, entries, indent)
    else:
        toc = next((i for i, l in enumerate(lines) if re.match(r'^\s+toc:\s*$', l)), None)
        if toc is None:
            raise SystemExit('myst.yml has no project.toc list to add notebooks to')
        key_indent = len(lines[toc]) - len(lines[toc].lstrip())
        item_indent, last = None, toc
        for i in range(toc + 1, len(lines)):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith('#'):
                continue
            indent = len(lines[i]) - len(lines[i].lstrip())
            if indent <= key_indent and not stripped.startswith('- '):
                break
            if item_indent is None and stripped.startswith('- '):
                item_indent = indent
            last = i
        if item_indent is None:
            item_indent = key_indent + 2
        lines[last + 1 : last + 1] = toc_block(section, entries, item_indent)

    text = '\n'.join(lines) + '\n'
    yaml.safe_load(text)  # fail loudly rather than write a broken myst.yml
    myst.write_text(text)
    return stale

#!/usr/bin/env python3
"""Sync notebooks from other GitHub repositories into this Cookbook.

The code lives in the notebook_sync package next to this file; see its
__init__.py for what a sync does. Run from anywhere:

    python .github/scripts/sync_external_notebooks.py

Requires git and PyYAML.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notebook_sync.sync import main  # noqa: E402

if __name__ == '__main__':
    main()

"""Sync notebooks from other GitHub repositories into this Cookbook.

Reads cookbook_sources.yml at the repository root and, for every listed notebook:

* copies the latest upstream version to external/<owner>/<repo>/<original path>,
  mirroring the upstream layout so relative paths inside the notebook keep working;
* copies the images the notebook's Markdown cells reference (Markdown images, HTML
  <img> tags and MyST {figure}/{image} directives), creating folders as needed;
* copies the local Python modules the notebook imports (or %runs), and the modules
  those import in turn, at the same relative paths so the imports keep working;
* copies files in the notebook's repository that its code (or those modules) names
  by a literal path, such as "data/aoi.geojson", up to 10 MB each;
* copies anything listed under the notebook's `include:` key: paths in its own
  repository, files or folders from other GitHub repositories, or any URL;
* skips automatically found files that match its `exclude:` patterns;
* rewrites links to other upstream files that were not copied so they point at
  GitHub instead of breaking;
* inserts a Markdown cell that credits the original notebook, its repository,
  license and the upstream commit it was copied from;
* lists the notebook in myst.yml inside a block delimited by marker comments.

The run is deterministic: if nothing changed upstream, nothing changes here.
Files that a removed source used to bring in are deleted.

Modules:
  manifest  reading cookbook_sources.yml
  upstream  fetching files from GitHub repositories and the web
  detect    finding the images, modules and data files a notebook needs
  notebook  editing notebook content (links, images, the Source note)
  toc       updating myst.yml
  sync      running it all

Run it with `python .github/scripts/sync_external_notebooks.py`. Requires git and PyYAML.
"""

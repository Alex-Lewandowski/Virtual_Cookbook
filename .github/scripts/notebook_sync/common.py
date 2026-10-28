"""Settings and logging shared by the rest of the package."""

# Files the sync manages, relative to the Cookbook repository root.
MANIFEST_FILE = 'cookbook_sources.yml'
MYST_FILE = 'myst.yml'
DEST_DIR = 'external'
LOCK_FILE = f'{DEST_DIR}/sources.lock.json'

DEFAULT_SECTION = 'Community Notebooks'


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # Shows up as a warning annotation on the workflow run.
    print(f'::warning::{msg}', flush=True)


def error(msg):
    print(f'::error::{msg}', flush=True)

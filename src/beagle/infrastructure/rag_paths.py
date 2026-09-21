"""Single source of truth for RAG database and staging paths.

B-5, B-6, B-16, B-19 audit fixes:
Provides call-time resolution of database roots and URIs based on
BEAGLE_KNOWLEDGE_DIR and BEAGLE_RAG_TIER environment variables.
"""

from __future__ import annotations

import os

from beagle.config.paths import get_data_root

# v1.2.0 (RG-6, BGL-009): resolve the RAG roots from the canonical data root
# instead of hardcoded host paths. get_data_root() honours $BEAGLE_DATA_ROOT,
# config.toml [paths].data_root, XDG_DATA_HOME, then ~/.beagle — so a clean
# install on any host lands in the right place.
#
# v13.22.4: these were module-level constants bound at IMPORT time. A test
# that imported beagle before monkeypatching $BEAGLE_DATA_ROOT therefore kept
# resolving to the operator's live ~/.beagle — and one did, replacing a
# ~20k-chunk index with two chunks from /tmp/pytest-of-server (2026-09-21).
# `db_root()` was already call-time, but `get_instance_rag_root()` was not,
# so the two disagreed under an env override. Both are call-time now.
LANCE_TABLE_NAME = "ast_code_chunks"


def get_instance_rag_root() -> str:
    """Return the instance RAG root, resolved at CALL time."""
    return str(get_data_root() / "instance_rag")


def get_main_rag_root() -> str:
    """Return the main RAG root, resolved at CALL time."""
    return str(get_data_root() / "main_rag")


# Deprecated import-time aliases. Kept for one release so an out-of-tree
# importer does not break; new code must call the functions above.
_INSTANCE_RAG_ROOT = get_instance_rag_root()
_MAIN_RAG_ROOT = get_main_rag_root()


def db_root(root: str | None = None) -> str:
    """Return canonical database root directory.

    Reads BEAGLE_KNOWLEDGE_DIR and BEAGLE_RAG_TIER at CALL time.
    Preserves symlinked roots without resolving them away.
    Normalizes trailing slashes.
    """
    if root is not None and str(root).strip():
        res = str(root)
    else:
        env_dir = os.environ.get("BEAGLE_KNOWLEDGE_DIR")
        if env_dir and env_dir.strip():
            res = env_dir
        else:
            tier = os.environ.get("BEAGLE_RAG_TIER", "instance")
            res = get_main_rag_root() if tier == "main" else get_instance_rag_root()

    # Normalize trailing slash and relative components, but keep symlinks intact
    res = os.path.normpath(res)
    return res


def lancedb_uri(root: str | None = None) -> str:
    """Return LanceDB directory URI."""
    return os.path.join(db_root(root), "lancedb")


def kuzu_uri(root: str | None = None) -> str:
    """Return Kùzu single-file database path."""
    base = db_root(root)
    if base.endswith("/"):
        base = base[:-1]
    return base + "_kuzu"


def staging_dir(override: str | None = None) -> str:
    """Return staging directory path created with mode 0700.

    Placed on the same filesystem as the live DB root so the swap can use
    atomic os.rename().  Defaults to a sibling of db_root named
    ``<db_root>.staging``.
    """
    if override is not None and str(override).strip():
        res = os.path.normpath(str(override))
    elif os.environ.get("BEAGLE_STAGING_DIR"):
        res = os.path.normpath(os.environ["BEAGLE_STAGING_DIR"])
    else:
        res = db_root() + ".staging"

    res = os.path.normpath(res)
    os.makedirs(res, mode=0o700, exist_ok=True)
    # Re-assert 0700 in case the directory already existed with looser perms
    os.chmod(res, 0o700)
    return res


def backup_dir(override: str | None = None) -> str:
    """Return backup directory path created with mode 0700.

    Placed on the same filesystem as the live DB root so the swap can use
    atomic os.rename().  Defaults to a sibling of db_root named
    ``<db_root>.backup``.
    """
    if override is not None and str(override).strip():
        res = os.path.normpath(str(override))
    elif os.environ.get("BEAGLE_RAG_BACKUP_DIR"):
        res = os.path.normpath(os.environ["BEAGLE_RAG_BACKUP_DIR"])
    elif os.environ.get("BEAGLE_KNOWLEDGE_DIR"):
        res = os.path.normpath(os.environ["BEAGLE_KNOWLEDGE_DIR"] + ".backup")
    else:
        res = db_root() + ".backup"

    res = os.path.normpath(res)
    os.makedirs(res, mode=0o700, exist_ok=True)
    # Re-assert 0700 in case the directory already existed with looser perms
    os.chmod(res, 0o700)
    return res

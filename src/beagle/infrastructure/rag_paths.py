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
_INSTANCE_RAG_ROOT = str(get_data_root() / "instance_rag")
_MAIN_RAG_ROOT = str(get_data_root() / "main_rag")
LANCE_TABLE_NAME = "ast_code_chunks"


def get_instance_rag_root() -> str:
    return _INSTANCE_RAG_ROOT


def get_main_rag_root() -> str:
    return _MAIN_RAG_ROOT


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
            res = _MAIN_RAG_ROOT if tier == "main" else _INSTANCE_RAG_ROOT

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

"""Shared pytest fixtures: live source + verification PostgreSQL instances.

The instances are provisioned via the bundled ``pgserver`` binaries so the
suite runs without Docker. Connection DSNs come from environment variables
with local socket defaults.
"""
from __future__ import annotations

import os
import pathlib
import secrets

import pgserver
import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_DSN = os.environ.get(
    "SOURCE_DSN", "postgresql://postgres@/appdb?host=/tmp/pgdata/source"
)
VERIFY_DSN = os.environ.get(
    "VERIFY_DSN", "postgresql://postgres@/postgres?host=/tmp/pgdata/verify"
)
os.environ.setdefault("VERIFY_CLEANUP", "detach")


@pytest.fixture(scope="session")
def source_dsn() -> str:
    # Ensure the demo source cluster exists and is reachable.
    pgserver.get_server("/tmp/pgdata/source", cleanup_mode=None)
    return SOURCE_DSN


@pytest.fixture(scope="session")
def verify_dsn() -> str:
    pgserver.get_server("/tmp/pgdata/verify", cleanup_mode=None)
    return VERIFY_DSN


@pytest.fixture()
def fresh_verify_database(verify_dsn):
    """Create/detach an isolated scratch database for low-level tests."""
    name = f"pytest_{secrets.token_hex(6)}"
    with psycopg.connect(verify_dsn, autocommit=True) as adm:
        adm.cursor().execute(f'CREATE DATABASE "{name}"')
    info = psycopg.conninfo.conninfo_to_dict(verify_dsn)
    info["dbname"] = name
    yield psycopg.conninfo.make_conninfo(**info), name
    # detach (sandbox) — never attempt a blocking drop here
    try:
        with psycopg.connect(verify_dsn, autocommit=True) as adm:
            adm.cursor().execute(
                f'ALTER DATABASE "{name}" RENAME TO "retired_{name}"'
            )
    except psycopg.Error:
        pass

"""Database access helpers.

The source connection is hardened so that it can only read catalog metadata:

* ``default_transaction_read_only = on``
* ``statement_timeout`` / ``lock_timeout`` bound every statement
* introspection queries only touch ``pg_catalog`` / ``information_schema``
* introspection never executes ``SELECT ... FROM <user table>``
"""
from __future__ import annotations

from contextlib import contextmanager

import psycopg


@contextmanager
def source_connection(dsn: str):
    """Open a strictly read-only connection to the source database."""
    conn = psycopg.connect(dsn, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute("SET statement_timeout = '15s'")
            cur.execute("SET lock_timeout = '5s'")
            # Paranoia: refuse to mutate even if transaction state is bypassed.
            cur.execute("SET idle_in_transaction_session_timeout = '30s'")
        conn.commit()
        yield conn
    finally:
        conn.close()


@contextmanager
def admin_connection(dsn: str, autocommit: bool = True):
    """Connection to the verification cluster's maintenance database."""
    conn = psycopg.connect(dsn, connect_timeout=10, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()

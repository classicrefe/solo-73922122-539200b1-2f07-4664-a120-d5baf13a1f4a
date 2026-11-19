"""Runtime configuration, all overridable via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # Source database: structure is read from catalogs only.
    source_dsn: str = os.environ.get(
        "SOURCE_DSN",
        "postgresql://app:app@source-db:5432/appdb",
    )
    # Administrative connection used to create/drop isolated verification
    # databases on the verification cluster. Never points at the source.
    verify_dsn: str = os.environ.get(
        "VERIFY_DSN",
        "postgresql://verifier:verifier@verify-db:5432/postgres",
    )
    max_tables: int = _int("MAX_TABLES", 4)
    max_rows_per_table: int = _int("MAX_ROWS_PER_TABLE", 20)
    job_ttl_seconds: int = _int("JOB_TTL_SECONDS", 3600)
    verify_db_prefix: str = os.environ.get("VERIFY_DB_PREFIX", "sample_verify_")
    # "drop"   : standard behaviour, DROP DATABASE after each job (default).
    # "detach" : do not drop; disconnect and rename the database aside. Needed
    #            only on sandboxed kernels that deny checkpoint signals, where
    #            DROP DATABASE would block. A normal postgres image uses "drop".
    verify_cleanup: str = os.environ.get("VERIFY_CLEANUP", "drop")


settings = Settings()

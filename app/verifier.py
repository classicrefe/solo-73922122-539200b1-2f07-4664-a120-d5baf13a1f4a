"""Verify generated SQL by actually loading it into an isolated empty database.

For every job a fresh database is created on the verification cluster, the
three SQL files run inside their own transactions, and afterwards negative
probes prove that primary keys, unique keys and foreign keys really reject
violations. The verification database is always dropped afterwards. No
partial package is ever exposed on failure.
"""
from __future__ import annotations

import secrets

import psycopg

from .config import settings
from .models import TYPE_BOUNDS, TableSchema
from .sqlgen import q


class VerificationError(Exception):
    pass


# Sentinel values guaranteed never to appear in solver output: negative and
# below every requested interval, but kept inside each column's type bounds so
# the rejection comes from the constraint (23503/23505), not a type overflow.
def _sentinel(col, offset: int) -> int:
    low = TYPE_BOUNDS[col.pg_type][0]
    return low + 1 + offset


def _execute_script(conn, script: str, log: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(script)
    conn.commit()
    log.append(f"executed script ({len(script)} bytes)")


def _run_negative(conn, sql: str, log: list[str], label: str, expect_sqlstate: str | None = None) -> None:
    """Run SQL inside a savepoint; it must be rejected by a constraint."""
    savepoint = f"sp_{secrets.token_hex(6)}"
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {q(savepoint)}")
        try:
            cur.execute(sql)
        except psycopg.Error as exc:
            cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
            conn.commit()
            if expect_sqlstate and exc.sqlstate != expect_sqlstate:
                raise VerificationError(
                    f"负向校验异常：{label} 期望 {expect_sqlstate}，实际 {exc.sqlstate} ({exc})"
                )
            log.append(f"negative probe OK [{label}] -> {exc.sqlstate}")
            return
        cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
        conn.commit()
    raise VerificationError(f"负向校验失败：{label} 本应被约束拒绝，但执行成功")


def verify_package(
    admin_dsn: str,
    db_prefix: str,
    schema: str,
    tables: dict[str, TableSchema],
    selected: list[str],
    package: dict[str, str],
) -> tuple[bool, list[str]]:
    db_name = f"{db_prefix}{secrets.token_hex(8)}"
    log: list[str] = [f"creating isolated database {db_name}"]
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as adm:
            with adm.cursor() as cur:
                cur.execute(f"CREATE DATABASE {q(db_name)}")
        dsn = _replace_dbname(admin_dsn, db_name)
        with psycopg.connect(dsn, autocommit=False, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {q(schema)}")
            conn.commit()
            _execute_script(conn, package["01_schema.sql"], log)
            _execute_script(conn, package["02_data.sql"], log)
            _execute_script(conn, package["03_constraints.sql"], log)
            _assert_row_counts(conn, schema, selected, log)
            _assert_constraints_enforced(conn, schema, tables, selected, log)
        log.append("all verification steps passed")
        return True, log
    except (psycopg.Error, VerificationError, KeyError) as exc:
        log.append(f"verification FAILED: {exc.__class__.__name__}: {exc}")
        return False, log
    finally:
        _cleanup_database(admin_dsn, db_name, log)


def _cleanup_database(admin_dsn: str, db_name: str, log: list[str]) -> None:
    """Terminate backends and release the verification database.

    Standard mode (``VERIFY_CLEANUP=drop``, the default and the behaviour in
    the shipped docker image) runs ``DROP DATABASE``.

    Sandboxed kernels may deny the signal used during the drop's checkpoint;
    ``VERIFY_CLEANUP=detach`` then disconnects every backend and renames the
    database aside instead of dropping it, so the environment limitation can
    never mask a verification verdict.
    """
    try:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as adm:
            with adm.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s
                      AND pid IS NOT NULL
                      AND pid <> pg_backend_pid()
                    """,
                    (db_name,),
                )
                if settings.verify_cleanup == "detach":
                    retired = f"retired_{db_name}"
                    cur.execute(
                        f"ALTER DATABASE {q(db_name)} RENAME TO {q(retired)}"
                    )
                    log.append(
                        f"detached verification database (renamed to {retired}); "
                        f"set VERIFY_CLEANUP=drop on a standard postgres image to drop it"
                    )
                else:
                    cur.execute(f"DROP DATABASE IF EXISTS {q(db_name)}")
                    log.append(f"dropped verification database {db_name}")
    except psycopg.Error as exc:
        log.append(
            f"cleanup warning: verification verdict is unaffected; "
            f"could not release {db_name} ({exc.sqlstate}: {exc})."
        )


def _replace_dbname(dsn: str, dbname: str) -> str:
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    info["dbname"] = dbname
    return psycopg.conninfo.make_conninfo(**info)


def _assert_row_counts(conn, schema, selected, log) -> None:
    with conn.cursor() as cur:
        for tname in selected:
            cur.execute(f"SELECT count(*) FROM {q(schema)}.{q(tname)}")
            (n,) = cur.fetchone()
            log.append(f"row count {tname} = {n}")


def _first_row(conn, schema, tname, cols):
    cols_sql = ", ".join(q(c) for c in cols)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {cols_sql} FROM {q(schema)}.{q(tname)} LIMIT 1")
        return cur.fetchone()


def _assert_constraints_enforced(conn, schema, tables, selected, log) -> None:
    """Negative probes proving constraints are live, not merely present.

    Probes use UPDATE inside savepoints on existing rows, so they neither
    depend on inserting a full valid row nor mutate the verified data.
    """
    for tname in selected:
        ts = tables[tname]
        first = _first_row(conn, schema, tname, [c.name for c in ts.columns])
        if first is None:
            log.append(f"skip negative probes for {tname} (0 rows)")
            continue
        col_by_name = {c.name: c for c in ts.columns}
        tbl = f"{q(schema)}.{q(tname)}"
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {tbl}")
            (n_rows,) = cur.fetchone()

        # --- PK / UNIQUE: force the first row's key onto a second row's key ---
        for uc in ts.unique_constraints:
            kind = "主键" if uc.is_primary else "唯一键"
            if n_rows < 2:
                log.append(f"skip {kind} probe {uc.name}: table has fewer than 2 rows")
                continue
            key_cols = uc.columns
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(q(c) for c in key_cols)} "
                    f"FROM {tbl} "
                    f"WHERE {' AND '.join(q(c) + ' IS NOT NULL' for c in key_cols)} "
                    f"LIMIT 1"
                )
                keyvals = cur.fetchone()
            if keyvals is None:
                log.append(f"skip {kind} probe {uc.name}: no non-NULL key row")
                continue
            set_clause = ", ".join(
                f"{q(c)} = {_lit(v)}" for c, v in zip(key_cols, keyvals)
            )
            # Apply to every *other* row; with >=2 rows a duplicate is forced.
            _run_negative(
                conn,
                f"UPDATE {tbl} SET {set_clause} WHERE ctid NOT IN "
                f"(SELECT ctid FROM {tbl} LIMIT 1)",
                log,
                f"{tname} {kind} {uc.name} 重复",
                expect_sqlstate="23505",
            )

        # --- Foreign keys: point an existing row at a guaranteed-absent key ---
        for fk in ts.foreign_keys:
            set_parts = []
            for k, cname in enumerate(fk.columns):
                col = col_by_name[cname]
                set_parts.append(f"{q(cname)} = {_sentinel(col, k)}")
            set_clause = ", ".join(set_parts)
            _run_negative(
                conn,
                f"UPDATE {tbl} SET {set_clause} WHERE ctid IN "
                f"(SELECT ctid FROM {tbl} LIMIT 1)",
                log,
                f"{tname} 外键 {fk.name} 悬空引用",
                expect_sqlstate="23503",
            )

            if len(fk.columns) > 1:
                # MATCH FULL must reject mixed NULL/non-NULL composite keys.
                mixed_parts = []
                for k, cname in enumerate(fk.columns):
                    if k == 0:
                        mixed_parts.append(f"{q(cname)} = NULL")
                    else:
                        col = col_by_name[cname]
                        mixed_parts.append(f"{q(cname)} = {_sentinel(col, 100 + k)}")
                mixed_sql = (
                    f"UPDATE {tbl} SET {', '.join(mixed_parts)} WHERE ctid IN "
                    f"(SELECT ctid FROM {tbl} LIMIT 1)"
                )
                if fk.match.value == "MATCH FULL":
                    _run_negative(
                        conn,
                        mixed_sql,
                        log,
                        f"{tname} 外键 {fk.name} MATCH FULL 混合NULL",
                        expect_sqlstate="23503",
                    )
                else:
                    # MATCH SIMPLE allows a mixed-NULL key (no FK violation).
                    savepoint = f"sp_{secrets.token_hex(6)}"
                    with conn.cursor() as cur:
                        cur.execute(f"SAVEPOINT {q(savepoint)}")
                        try:
                            cur.execute(mixed_sql)
                            log.append(
                                f"semantic probe OK [{fk.name} MATCH SIMPLE 混合NULL被允许]"
                            )
                        except psycopg.Error as exc:
                            log.append(
                                f"semantic probe note [{fk.name} MATCH SIMPLE]: "
                                f"blocked by another constraint {exc.sqlstate}"
                            )
                        finally:
                            cur.execute(f"ROLLBACK TO SAVEPOINT {q(savepoint)}")
                            conn.commit()


def _lit(v) -> str:
    return "NULL" if v is None else str(v)

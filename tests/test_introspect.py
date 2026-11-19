"""Introspection tests against the live source catalogs (no row reads)."""
from __future__ import annotations

import psycopg
import pytest

from app.db import source_connection
from app.introspect import (
    IntrospectionError,
    introspect_tables,
    list_base_tables,
)


def test_lists_only_base_tables(source_dsn):
    with source_connection(source_dsn) as conn:
        base, others = list_base_tables(conn, "public")
    assert set(base) == {"dept", "employee", "project", "assignment"}
    assert others == []


def test_introspects_full_structure(source_dsn):
    with source_connection(source_dsn) as conn:
        schemas = introspect_tables(conn, "public", ["dept", "project"])
    dept = schemas["dept"]
    assert [c.pg_type for c in dept.columns] == [
        "integer",
        "smallint",
        "integer",
        "integer",
    ]
    assert dept.column("parent_dept").nullable is True
    assert dept.column("dept_id").nullable is False
    pk = dept.pk
    assert pk.columns == ["dept_id"]
    uk = next(u for u in dept.unique_constraints if not u.is_primary)
    assert uk.columns == ["dept_id", "dept_code"]
    fk = next(f for f in dept.foreign_keys if f.name == "dept_mgr_fk")
    assert (fk.ref_table, fk.columns, fk.ref_columns) == (
        "employee",
        ["mgr_emp"],
        ["emp_id"],
    )
    project = schemas["project"]
    full_fk = next(f for f in project.foreign_keys if f.name == "project_dept_fk")
    assert full_fk.columns == ["dept_id", "dept_code"]
    assert full_fk.match.value == "MATCH FULL"
    assert full_fk.not_deferrable is True


def test_missing_table_rejected(source_dsn):
    with source_connection(source_dsn) as conn:
        with pytest.raises(IntrospectionError) as exc:
            introspect_tables(conn, "public", ["no_such_table"])
    assert any("不存在" in r for _, r in exc.value.rejections)


def test_unsupported_type_rejected(source_dsn, fresh_verify_database):
    dsn, _ = fresh_verify_database
    with psycopg.connect(dsn, autocommit=True) as c:
        c.cursor().execute("CREATE TABLE t_text (id integer PRIMARY KEY, note text)")
    # introspect through the scratch cluster directly
    import app.db as dbmod
    from app.introspect import introspect_tables as intro

    with dbmod.source_connection(dsn) as conn:
        with pytest.raises(IntrospectionError) as exc:
            intro(conn, "public", ["t_text"])
    assert any("text" in r and "整数列" in r for _, r in exc.value.rejections)


def test_check_and_default_rejected(source_dsn, fresh_verify_database):
    dsn, _ = fresh_verify_database
    with psycopg.connect(dsn, autocommit=True) as c:
        c.cursor().execute(
            """
            CREATE TABLE t_chk (
                id integer PRIMARY KEY,
                age integer NOT NULL DEFAULT 0 CHECK (age >= 0)
            )
            """
        )
    import app.db as dbmod
    from app.introspect import introspect_tables as intro

    with dbmod.source_connection(dsn) as conn:
        with pytest.raises(IntrospectionError) as exc:
            intro(conn, "public", ["t_chk"])
    reasons = " ".join(r for _, r in exc.value.rejections)
    assert "CHECK" in reasons
    assert "DEFAULT" in reasons


def test_view_rejected(source_dsn, fresh_verify_database):
    dsn, _ = fresh_verify_database
    with psycopg.connect(dsn, autocommit=True) as c:
        c.cursor().execute("CREATE TABLE t_base (id integer PRIMARY KEY)")
        c.cursor().execute("CREATE VIEW v1 AS SELECT * FROM t_base")
    import app.db as dbmod
    from app.introspect import introspect_tables as intro

    with dbmod.source_connection(dsn) as conn:
        with pytest.raises(IntrospectionError) as exc:
            intro(conn, "public", ["v1"])
    assert any("视图" in r for _, r in exc.value.rejections)

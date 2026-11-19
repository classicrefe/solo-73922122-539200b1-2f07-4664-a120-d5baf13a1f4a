"""SQL rendering guarantees: deferrability preserved, cycles via post-data FKs."""
from __future__ import annotations

from app.models import (
    Column,
    ForeignKey,
    IntRange,
    MatchType,
    TableSchema,
    TableSpec,
    UniqueConstraint,
)
from app.solver import Generator
from app.sqlgen import build_package


def _col(name, nullable=False, pg_type="integer"):
    return Column(
        name=name,
        pg_type=pg_type,
        ordinal=1,
        nullable=nullable,
        has_default=False,
        is_identity=False,
        is_generated=False,
    )


def test_not_deferrable_fk_stays_immediate():
    parent = TableSchema(
        schema_name="public",
        name="p",
        columns=[_col("id")],
        unique_constraints=[
            UniqueConstraint(name="pk_p", columns=["id"], is_primary=True)
        ],
        foreign_keys=[],
    )
    child = TableSchema(
        schema_name="public",
        name="c",
        columns=[_col("id"), _col("pid")],
        unique_constraints=[
            UniqueConstraint(name="pk_c", columns=["id"], is_primary=True)
        ],
        foreign_keys=[
            ForeignKey(
                name="c_fk",
                columns=["pid"],
                ref_table="p",
                ref_columns=["id"],
                match=MatchType.SIMPLE,
                on_update="NO ACTION",
                on_delete="NO ACTION",
                not_deferrable=True,
                initially_deferred=False,
            )
        ],
    )
    specs = {
        "p": TableSpec(table="p", rows=2, ranges={"id": IntRange(low=1, high=2)}),
        "c": TableSpec(
            table="c",
            rows=2,
            ranges={"id": IntRange(low=10, high=11), "pid": IntRange(low=1, high=2)},
        ),
    }
    data = Generator({"p": parent, "c": child}, specs).solve()
    pkg = build_package({"p": parent, "c": child}, ["p", "c"], specs, data)
    fk_ddl = [
        line for line in pkg["03_constraints.sql"].splitlines()
        if line.startswith("ALTER TABLE")
    ][0]
    assert "NOT DEFERRABLE" in fk_ddl
    assert "INITIALLY DEFERRED" not in fk_ddl
    # No inline FK in the CREATE TABLE step (cycles handled post-data).
    assert "FOREIGN KEY" not in pkg["01_schema.sql"]


def test_deferrable_fk_preserved_verbatim():
    parent = TableSchema(
        schema_name="public",
        name="p",
        columns=[_col("id")],
        unique_constraints=[
            UniqueConstraint(name="pk_p", columns=["id"], is_primary=True)
        ],
        foreign_keys=[],
    )
    child = TableSchema(
        schema_name="public",
        name="c",
        columns=[_col("id"), _col("pid")],
        unique_constraints=[
            UniqueConstraint(name="pk_c", columns=["id"], is_primary=True)
        ],
        foreign_keys=[
            ForeignKey(
                name="c_fk",
                columns=["pid"],
                ref_table="p",
                ref_columns=["id"],
                match=MatchType.SIMPLE,
                on_update="NO ACTION",
                on_delete="NO ACTION",
                not_deferrable=False,
                initially_deferred=True,
            )
        ],
    )
    specs = {
        "p": TableSpec(table="p", rows=2, ranges={"id": IntRange(low=1, high=2)}),
        "c": TableSpec(
            table="c",
            rows=2,
            ranges={"id": IntRange(low=10, high=11), "pid": IntRange(low=1, high=2)},
        ),
    }
    data = Generator({"p": parent, "c": child}, specs).solve()
    pkg = build_package({"p": parent, "c": child}, ["p", "c"], specs, data)
    fk_ddl = [
        line for line in pkg["03_constraints.sql"].splitlines()
        if line.startswith("ALTER TABLE")
    ][0]
    assert "DEFERRABLE INITIALLY DEFERRED" in fk_ddl
    assert "NOT DEFERRABLE" not in fk_ddl


def test_null_emitted_explicitly_and_quoted():
    ts = TableSchema(
        schema_name="public",
        name="weird name",
        columns=[_col("id"), _col("v", nullable=True)],
        unique_constraints=[
            UniqueConstraint(name="pk", columns=["id"], is_primary=True)
        ],
        foreign_keys=[],
    )
    specs = {
        "weird name": TableSpec(
            table="weird name",
            rows=2,
            ranges={"id": IntRange(low=1, high=2), "v": IntRange(low=1, high=1)},
        )
    }
    data = Generator({"weird name": ts}, specs).solve()
    pkg = build_package({"weird name": ts}, ["weird name"], specs, data)
    assert '"weird name"' in pkg["01_schema.sql"]
    # A non-nullable PK with only 2 distinct values for 2 rows is feasible;
    # verify explicit NULL rendering with a dedicated nullable-only scenario.
    null_ts = TableSchema(
        schema_name="public",
        name="n",
        columns=[_col("id"), _col("v", nullable=True)],
        unique_constraints=[
            UniqueConstraint(name="pk_n", columns=["id"], is_primary=True)
        ],
        foreign_keys=[],
    )
    # v has a single-point interval but a unique index: extra rows must use NULL
    # (SQL UNIQUE allows repeated NULLs), exercising explicit NULL rendering.
    null_ts.unique_constraints.append(
        UniqueConstraint(name="uk_n_v", columns=["v"], is_primary=False)
    )
    null_specs = {
        "n": TableSpec(
            table="n",
            rows=3,
            ranges={"id": IntRange(low=1, high=3), "v": IntRange(low=7, high=7)},
        )
    }
    null_data = Generator({"n": null_ts}, null_specs).solve()
    null_pkg = build_package({"n": null_ts}, ["n"], null_specs, null_data)
    assert any(r["v"] is None for r in null_data["n"])
    assert "NULL" in null_pkg["02_data.sql"]

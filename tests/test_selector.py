"""Pure unit tests for FK closure expansion and request validation."""
from __future__ import annotations

import pytest

from app.introspect import IntrospectionError
from app.models import (
    Column,
    IntRange,
    TableSchema,
    TableSpec,
)
from app.selector import expand_fk_closure, validate_request


def test_closure_follows_transitive_edges_and_cycles():
    graph = {
        "a": ["b"],
        "b": ["c"],
        "c": ["a"],  # cycle back to a
    }
    out = expand_fk_closure(["a"], graph, max_tables=4)
    assert set(out) == {"a", "b", "c"}


def test_closure_respects_max_tables():
    graph = {"a": ["b"], "b": ["c"], "c": ["d"], "d": ["e"]}
    with pytest.raises(IntrospectionError) as exc:
        expand_fk_closure(["a"], graph, max_tables=4)
    assert "超过至多 4 张表" in exc.value.rejections[0][1]


def test_closure_dedupes_shared_targets():
    graph = {"a": ["x"], "b": ["x"], "x": []}
    out = expand_fk_closure(["a", "b"], graph, max_tables=4)
    assert sorted(out) == ["a", "b", "x"]


def _schema():
    return TableSchema(
        schema_name="public",
        name="t",
        columns=[
            Column(
                name="id",
                pg_type="integer",
                ordinal=1,
                nullable=False,
                has_default=False,
                is_identity=False,
                is_generated=False,
            ),
            Column(
                name="v",
                pg_type="smallint",
                ordinal=2,
                nullable=True,
                has_default=False,
                is_identity=False,
                is_generated=False,
            ),
        ],
        unique_constraints=[],
        foreign_keys=[],
    )


def test_validate_request_detects_missing_range_and_bad_bounds():
    ts = _schema()
    spec = TableSpec(
        table="t",
        rows=3,
        ranges={"id": IntRange(low=1, high=2)},  # missing v; ids only 2 values
    )
    req_specs = [spec]
    from app.models import GenerateRequest

    req = GenerateRequest(schema_name="public", tables=req_specs)
    problems = dict(validate_request(req, {"t": ts}, max_rows=20))
    assert any("v" in r and "区间" in r for r in problems.values())


def test_validate_request_rejects_out_of_type_range():
    ts = _schema()
    spec = TableSpec(
        table="t",
        rows=1,
        ranges={
            "id": IntRange(low=1, high=2),
            "v": IntRange(low=-40000, high=40000),  # beyond smallint
        },
    )
    from app.models import GenerateRequest

    req = GenerateRequest(schema_name="public", tables=[spec])
    problems = validate_request(req, {"t": ts}, max_rows=20)
    assert any("smallint" in r for _, r in problems)


def test_validate_request_rejects_row_count_above_cap():
    # Pydantic enforces the hard 0..20 field bound.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TableSpec(
            table="t",
            rows=21,
            ranges={
                "id": IntRange(low=1, high=100),
                "v": IntRange(low=1, high=100),
            },
        )

    # The service-level cap check rejects a count beyond a smaller configured cap.
    ts = _schema()
    spec = TableSpec(
        table="t",
        rows=15,
        ranges={
            "id": IntRange(low=1, high=100),
            "v": IntRange(low=1, high=100),
        },
    )
    from app.models import GenerateRequest

    req = GenerateRequest(schema_name="public", tables=[spec])
    problems = validate_request(req, {"t": ts}, max_rows=10)
    assert any("行数" in r for _, r in problems)

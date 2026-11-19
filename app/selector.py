"""Table selection with foreign-key closure and request validation."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import IntRange
from .introspect import IntrospectionError

if TYPE_CHECKING:
    from .models import GenerateRequest, TableSchema


def expand_fk_closure(
    requested: list[str],
    fk_graph: dict[str, list[str]],
    max_tables: int,
) -> list[str]:
    """Expand the requested set so every FK target table is included.

    ``fk_graph`` maps a table name to the names of tables its foreign keys
    reference. The resulting set contains at most ``max_tables`` tables and
    preserves a deterministic order. The graph is catalog-driven; row data is
    irrelevant.
    """
    selected: list[str] = []
    queue = list(requested)
    seen = set()
    while queue:
        t = queue.pop(0)
        if t in seen:
            continue
        seen.add(t)
        selected.append(t)
        for target in fk_graph.get(t, []):
            if target not in seen:
                queue.append(target)
    if len(selected) > max_tables:
        detail = ", ".join(sorted(selected))
        raise IntrospectionError(
            [
                (
                    "(selection)",
                    f"选择的表及其外键目标共 {len(selected)} 张（{detail}），"
                    f"超过至多 {max_tables} 张表的限制；请缩小选择范围",
                )
            ]
        )
    return selected


def validate_request(
    req: GenerateRequest,
    schemas: dict[str, TableSchema],
    max_rows: int,
) -> list[tuple[str, str]]:
    """Validate row counts and per-column closed intervals against catalogs."""
    problems: list[tuple[str, str]] = []
    seen_tables: set[str] = set()
    for spec in req.tables:
        if spec.table in seen_tables:
            problems.append((spec.table, "请求中重复指定该表"))
        seen_tables.add(spec.table)
        if not (0 <= spec.rows <= max_rows):
            problems.append((spec.table, f"行数须在 0～{max_rows} 之间，实际 {spec.rows}"))
        ts = schemas[spec.table]
        col_map = {c.name: c for c in ts.columns}
        for missing in set(col_map) - set(spec.ranges):
            problems.append((spec.table, f"缺少列 {missing} 的闭区间声明"))
        for col_name, rng in spec.ranges.items():
            if col_name not in col_map:
                problems.append((spec.table, f"区间指定了不存在的列 {col_name}"))
                continue
            col = col_map[col_name]
            try:
                IntRange(low=rng.low, high=rng.high).validated(col)
            except ValueError as exc:
                problems.append((f"{spec.table}.{col_name}", str(exc)))
    return problems

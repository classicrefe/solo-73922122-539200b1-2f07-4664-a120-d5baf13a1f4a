"""Read-only schema introspection over PostgreSQL catalogs.

Only ``pg_catalog`` / ``information_schema`` views are queried. No user table
is ever scanned. Any structure outside the supported feature set produces a
structured rejection instead of being silently ignored.
"""
from __future__ import annotations

from .models import (
    Column,
    ForeignKey,
    MatchType,
    TableSchema,
    UniqueConstraint,
    INT_TYPES,
)

# relkind values we accept for ordinary base tables.
_SUPPORTED_RELKIND = "r"


class IntrospectionError(Exception):
    """Raised when the source schema contains an unsupported structure."""

    def __init__(self, rejections: list[tuple[str, str]]):
        self.rejections = rejections
        super().__init__("; ".join(f"{obj}: {reason}" for obj, reason in rejections))


def list_base_tables(conn, schema_name: str) -> list[str]:
    """Return ordinary base-table names in a schema (catalogs only)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relkind IN ('r', 'v', 'm', 'f', 'p')
            ORDER BY c.relname
            """,
            (schema_name,),
        )
        all_rels = [r[0] for r in cur.fetchall()]
        cur.execute(
            """
            SELECT c.relname
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r'
            ORDER BY c.relname
            """,
            (schema_name,),
        )
        base = [r[0] for r in cur.fetchall()]
    # Surface non-base relations so callers can explain what was excluded.
    return base, [r for r in all_rels if r not in base]


def _reject_unsupported_relations(cur, schema: str, tables: list[str]) -> list[tuple[str, str]]:
    rej: list[tuple[str, str]] = []
    cur.execute(
        """
        SELECT c.relname, c.relkind,
               EXISTS(SELECT 1 FROM pg_catalog.pg_inherits h WHERE h.inhparent = c.oid) AS is_parent,
               EXISTS(SELECT 1 FROM pg_catalog.pg_inherits h WHERE h.inhrelid = c.oid) AS is_partition,
               EXISTS(
                   SELECT 1 FROM pg_catalog.pg_rewrite r
                   WHERE r.ev_class = c.oid
                     AND r.ev_type IN ('2', '3', '4')
               ) AS has_rule
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = ANY(%s)
        """,
        (schema, tables),
    )
    kind_name = {
        "v": "视图(view)",
        "m": "物化视图(materialized view)",
        "f": "外部表(foreign table)",
        "p": "分区表(partitioned table)",
    }
    for name, kind, is_parent, is_partition, has_rule in cur.fetchall():
        obj = f"{schema}.{name}"
        if kind != _SUPPORTED_RELKIND:
            rej.append((obj, f"不支持的关系类型 {kind_name.get(kind, kind)}，仅支持普通基表"))
        if is_parent:
            rej.append((obj, "不支持继承/分区父表（存在 pg_inherits 子表）"))
        if is_partition:
            rej.append((obj, "不支持分区子表（该表继承自父表）"))
        if has_rule:
            rej.append((obj, "不支持带 ON INSERT/UPDATE/DELETE 规则(rule)的表"))
    return rej


def _introspect_columns(cur, schema: str, table: str) -> tuple[list[Column], list[tuple[str, str]]]:
    rej: list[tuple[str, str]] = []
    cur.execute(
        """
        SELECT a.attname,
               a.atttypid,
               a.attnum,
               NOT a.attnotnull AS nullable,
               pg_get_expr(ad.adbin, ad.adrelid) IS NOT NULL AS has_default,
               a.attidentity,
               a.attgenerated
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef ad
          ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (schema, table),
    )
    cols: list[Column] = []
    for name, typoid, ordinal, nullable, has_default, identity, generated in cur.fetchall():
        obj = f"{schema}.{table}.{name}"
        if typoid not in INT_TYPES:
            cur.execute("SELECT format_type(%s, NULL)", (typoid,))
            type_name = cur.fetchone()[0]
            rej.append(
                (obj, f"不支持的列类型 {type_name}，仅支持 smallint/integer/bigint 整数列")
            )
            continue
        if generated == "s":
            rej.append((obj, "不支持 GENERATED 存储生成列"))
        if identity in ("a", "d"):
            rej.append(
                (
                    obj,
                    "不支持 IDENTITY 标识列（样本需显式提供全部列值），请改用普通 integer 主键",
                )
            )
        if has_default:
            rej.append((obj, "不支持列 DEFAULT 默认值（样本数据显式提供每一列，默认值无意义且会掩盖缺失）"))
        cols.append(
            Column(
                name=name,
                pg_type=INT_TYPES[typoid],
                ordinal=ordinal,
                nullable=nullable,
                has_default=has_default,
                is_identity=identity in ("a", "d"),
                is_generated=generated == "s",
            )
        )
    return cols, rej


def _introspect_constraints(
    cur, schema: str, table: str, col_names: set[str]
) -> tuple[list[UniqueConstraint], list[ForeignKey], list[tuple[str, str]]]:
    rej: list[tuple[str, str]] = []
    uniques: list[UniqueConstraint] = []
    fks: list[ForeignKey] = []

    cur.execute(
        """
        SELECT con.conname, con.contype,
               (SELECT array_agg(a.attname ORDER BY u.ord)
                  FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
                  JOIN pg_catalog.pg_attribute a
                    ON a.attrelid = con.conrelid AND a.attnum = u.attnum) AS cols,
               con.conindid,
               con.condeferrable, con.condeferred, con.confmatchtype,
               con.confupdtype, con.confdeltype,
               (SELECT array_agg(a2.attname ORDER BY u2.ord)
                  FROM unnest(con.confkey) WITH ORDINALITY AS u2(attnum, ord)
                  JOIN pg_catalog.pg_attribute a2
                    ON a2.attrelid = con.confrelid AND a2.attnum = u2.attnum) AS ref_cols,
               fn.nspname AS fschema, fc.relname AS ftable,
               pg_get_constraintdef(con.oid) AS condef
        FROM pg_catalog.pg_constraint con
        JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
        LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        ORDER BY con.conname
        """,
        (schema, table),
    )
    rows = cur.fetchall()
    for (
        conname,
        contype,
        cols,
        conindid,
        deferrable,
        deferred,
        matchtype,
        upd,
        del_,
        ref_cols,
        fschema,
        ftable,
        condef,
    ) in rows:
        obj = f"{schema}.{table}.{conname}"
        if contype == "c":
            rej.append((obj, f"不支持 CHECK 约束: {condef}"))
        elif contype == "x":
            rej.append((obj, "不支持 EXCLUDE 排他约束"))
        elif contype in ("p", "u"):
            if any(c not in col_names for c in cols):
                rej.append((obj, "唯一/主键约束包含非整数列（已因类型被拒绝），无法保留该约束"))
                continue
            uniques.append(
                UniqueConstraint(name=conname, columns=list(cols), is_primary=(contype == "p"))
            )
        elif contype == "f":
            if fschema != schema:
                rej.append((obj, f"外键跨 schema 引用 {fschema}.{ftable}，仅支持同 schema 内关联"))
                continue
            if any(c not in col_names for c in cols) or any(c is None for c in ref_cols or []):
                rej.append((obj, "外键包含非整数或无法解析的列"))
                continue
            match_map = {
                "s": MatchType.SIMPLE,
                "f": MatchType.FULL,
                "p": None,  # MATCH PARTIAL
            }
            match = match_map.get(matchtype)
            if match is None:
                rej.append((obj, "不支持 MATCH PARTIAL 外键（PostgreSQL 本身亦未实现），仅支持 MATCH SIMPLE / MATCH FULL"))
                continue
            action_map = {
                "a": "NO ACTION",
                "r": "RESTRICT",
                "c": "CASCADE",
                "n": "SET NULL",
                "d": "SET DEFAULT",
            }
            if upd == "d" or del_ == "d":
                rej.append((obj, "不支持 ON ... SET DEFAULT 外键动作（不生成默认值）"))
                continue
            fks.append(
                ForeignKey(
                    name=conname,
                    columns=list(cols),
                    ref_table=ftable,
                    ref_columns=list(ref_cols),
                    match=match,
                    on_update=action_map[upd],
                    on_delete=action_map[del_],
                    not_deferrable=not deferrable,
                    initially_deferred=bool(deferred),
                )
            )
        # contype 't' (trigger constraint) handled below via trigger scan
    return uniques, fks, rej


def _reject_triggers(cur, schema: str, table: str) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT t.tgname
        FROM pg_catalog.pg_trigger t
        JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s AND NOT t.tgisinternal
        """,
        (schema, table),
    )
    return [
        (f"{schema}.{table}.{name}", "不支持用户自定义触发器(trigger)")
        for (name,) in cur.fetchall()
    ]


def _reject_unique_indexes(cur, schema: str, table: str) -> list[tuple[str, str]]:
    """Reject unique indexes not backed by a constraint (partial/expression)."""
    cur.execute(
        """
        SELECT i.relname, ix.indisunique, ix.indisprimary,
               pg_get_expr(ix.indpred, ix.indrelid)
        FROM pg_catalog.pg_index ix
        JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
        JOIN pg_catalog.pg_class c ON c.oid = ix.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
          AND ix.indisunique
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.pg_constraint con WHERE con.conindid = ix.indexrelid
          )
        """,
        (schema, table),
    )
    out: list[tuple[str, str]] = []
    for name, unique, primary, pred in cur.fetchall():
        extra = "（部分索引带 WHERE 谓词）" if pred else "（非约束支持的唯一索引，可能含表达式列）"
        out.append((f"{schema}.{table}.{name}", f"不支持独立唯一索引 {extra}，请改用普通 UNIQUE 约束"))
    return out


def introspect_tables(conn, schema_name: str, tables: list[str]) -> dict[str, TableSchema]:
    """Introspect a closed set of tables. Raises IntrospectionError on rejection."""
    rejections: list[tuple[str, str]] = []
    with conn.cursor() as cur:
        # Existence check first.
        cur.execute(
            """
            SELECT c.relname
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = ANY(%s)
            """,
            (schema_name, tables),
        )
        found = {r[0] for r in cur.fetchall()}
        for t in tables:
            if t not in found:
                rejections.append((f"{schema_name}.{t}", "源库中不存在该表"))
        if rejections:
            raise IntrospectionError(rejections)

        rejections.extend(_reject_unsupported_relations(cur, schema_name, tables))

        result: dict[str, TableSchema] = {}
        for table in tables:
            cols, col_rej = _introspect_columns(cur, schema_name, table)
            rejections.extend(col_rej)
            uniques, fks, con_rej = _introspect_constraints(
                cur, schema_name, table, {c.name for c in cols}
            )
            rejections.extend(con_rej)
            rejections.extend(_reject_triggers(cur, schema_name, table))
            rejections.extend(_reject_unique_indexes(cur, schema_name, table))

            # Primary key ordinal annotation.
            pk = next((u for u in uniques if u.is_primary), None)
            if pk:
                order = {name: i + 1 for i, name in enumerate(pk.columns)}
                for c in cols:
                    c.ord_in_pk = order.get(c.name)
            result[table] = TableSchema(
                schema_name=schema_name,
                name=table,
                columns=cols,
                unique_constraints=uniques,
                foreign_keys=fks,
            )

    if rejections:
        raise IntrospectionError(sorted(set(rejections)))
    return result


def read_fk_graph(conn, schema_name: str, tables: list[str]) -> dict[str, list[str]]:
    """Return table -> referenced table names within the same schema.

    A lightweight catalog read used solely to compute the FK closure before
    the full (strict) introspection of the closed set. No row data is read.
    """
    graph: dict[str, list[str]] = {t: [] for t in tables}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname AS child, fc.relname AS parent, fn.nspname AS pschema
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class c ON c.oid = con.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
            JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
            WHERE con.contype = 'f'
              AND n.nspname = %s
              AND c.relname = ANY(%s)
            """,
            (schema_name, tables),
        )
        for child, parent, pschema in cur.fetchall():
            graph.setdefault(child, []).append(parent)
            # Cross-schema targets are recorded; strict introspection rejects
            # them later with a precise reason.
            graph.setdefault(parent, [])
            _ = pschema
    return graph

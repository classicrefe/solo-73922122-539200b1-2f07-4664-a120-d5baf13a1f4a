"""合装（co-load）：把两份独立生成且均已验证成功的样本合并为一份样本。

规则（与需求严格对应）：

* 第一份样本（job_a）的全部值原样保留；
* 第二份样本（job_b）只允许在整数列类型边界内改写两类单元格：主键列、
  以及构成有效引用（完整非 NULL 键）的外键列；其余单元格的值一律不变；
* NULL 位置不变：原为 NULL 的单元格保持 NULL，非 NULL 的保持非 NULL；
* 每条有效引用仍指向原来那条记录的合并版（含复合键、自引用与循环引用）；
* 合并后主键/唯一键/外键/NOT NULL 全部约束必须同时成立。

编码方式：第二份样本的可改写单元格成为 z3 整数变量（域 = 整数类型边界，
并避开验证器负向探测的哨兵值），其余单元格是常量；NULL 位置静态固定，
因此 NULL 语义由构造保证。唯一键按 SQL NULL 语义成对断言；每条有效引用
用等式把外键单元格绑定到被引用记录主键的合并值。软约束偏好「尽量保留
第二份样本原值」。无解与超时分别抛出 ``MergeUnsatisfiable`` /
``MergeTimeout``，绝不产出部分结果。
"""
from __future__ import annotations

from dataclasses import dataclass

import z3

from .models import TYPE_BOUNDS, TableSchema
from .solver import Z3_LOCK, Group, Row
from .verifier import probe_sentinel_value

#: 合装求解的时间预算（毫秒）。超时与无解严格区分。
SOLVER_TIMEOUT_MS = 30_000


class MergeUnsatisfiable(Exception):
    """在只允许改写第二份样本主键/有效引用外键列的限制下无可行合装。"""

    def __init__(self, groups: list[Group]):
        self.groups = groups
        super().__init__("; ".join(g.label for g in groups))


class MergeTimeout(Exception):
    """z3 限时内无法判定合装可行性（unknown）；不是无解结论。"""


class MergeInconsistent(Exception):
    """来源作业的已验证数据内部不一致（理论上不可达，防御性检查）。"""


@dataclass
class _ValidRef:
    """一条有效引用：第二份样本某行的完整非 NULL 外键键及其被引用记录。"""

    fk_name: str
    ref_table: str
    ref_row: int  # index into data_b[ref_table]
    local_cols: list[str]
    ref_cols: list[str]


def check_structure_compatible(
    schema_name_a: str,
    schemas_a: dict[str, TableSchema],
    schema_name_b: str,
    schemas_b: dict[str, TableSchema],
) -> list[tuple[str, str]]:
    """两份作业的结构必须完全相同（schema、表集合与每表结构定义）。"""
    problems: list[tuple[str, str]] = []
    if schema_name_a != schema_name_b:
        problems.append(
            ("(schema)", f"schema 不同：{schema_name_a} 与 {schema_name_b}")
        )
        return problems
    names_a, names_b = sorted(schemas_a), sorted(schemas_b)
    if names_a != names_b:
        problems.append(
            ("(tables)", f"表集合不同：{names_a} 与 {names_b}")
        )
        return problems
    for t in names_a:
        if schemas_a[t].model_dump() != schemas_b[t].model_dump():
            problems.append(
                (t, "两次作业内省到的表结构不一致（列/约束定义不同）")
            )
    return problems


def check_merge_eligible(
    schemas: dict[str, TableSchema],
    data_a: dict[str, list[Row]],
    data_b: dict[str, list[Row]],
    max_tables: int,
    max_rows: int,
) -> list[tuple[str, str]]:
    """合装资格：至多 max_tables 张表、每份样本每表至多 max_rows 行、外键均指向主键。"""
    problems: list[tuple[str, str]] = []
    if len(schemas) > max_tables:
        problems.append(
            (
                "(tables)",
                f"合装限至多 {max_tables} 张表，实际 {len(schemas)} 张：{sorted(schemas)}",
            )
        )
    for t in sorted(schemas):
        na, nb = len(data_a[t]), len(data_b[t])
        if na > max_rows or nb > max_rows:
            problems.append(
                (t, f"合装限每份样本每表至多 {max_rows} 行，实际 {na}/{nb} 行")
            )
    for t in sorted(schemas):
        for fk in schemas[t].foreign_keys:
            ref_pk = schemas[fk.ref_table].pk
            if ref_pk is None or set(fk.ref_columns) != set(ref_pk.columns):
                problems.append(
                    (
                        f"{t}.{fk.name}",
                        f"外键 ({', '.join(fk.columns)}) -> "
                        f"{fk.ref_table}({', '.join(fk.ref_columns)}) 未指向主键；"
                        "合装要求所有外键均指向主键",
                    )
                )
    return problems


def _probe_sentinel_values(schemas: dict[str, TableSchema]) -> set[int]:
    """验证器负向探测会把外键改成的哨兵值；改写后的键不得占用这些值。"""
    bad: set[int] = set()
    for ts in schemas.values():
        for fk in ts.foreign_keys:
            for k, cname in enumerate(fk.columns):
                col = ts.column(cname)
                bad.add(probe_sentinel_value(col, k))
                if len(fk.columns) > 1:
                    bad.add(probe_sentinel_value(col, 100 + k))
    return bad


class MergeSolver:
    """Encode and solve the co-load problem for one pair of verified samples.

    Only second-sample cells that are (a) primary-key columns or (b) part of
    a fully non-NULL (valid) foreign-key reference become z3 variables; every
    other cell is a constant equal to its original value. NULL placement is
    therefore preserved by construction.
    """

    def __init__(
        self,
        schemas: dict[str, TableSchema],
        data_a: dict[str, list[Row]],
        data_b: dict[str, list[Row]],
    ):
        self.schemas = schemas
        self.data_a = data_a
        self.data_b = data_b
        self.vars: dict[tuple[str, int, str], z3.ArithRef] = {}
        self.valid_refs: dict[tuple[str, int], list[_ValidRef]] = {}
        self.groups: list[Group] = []
        self._group_keys: set[str] = set()

    # ------------------------------------------------------------------ utils
    def _group(
        self, key: str, label: str, kind: str, refs: list[str] | None = None
    ) -> Group:
        if key in self._group_keys:
            return next(g for g in self.groups if g.key == key)
        g = Group(key=key, label=label, kind=kind, refs=refs or [])
        self._group_keys.add(key)
        self.groups.append(g)
        return g

    # --------------------------------------------------------------- analysis
    def _analyse(self) -> None:
        """Classify second-sample cells and resolve every valid reference."""
        for t, ts in self.schemas.items():
            pk_cols = set(ts.pk.columns) if ts.pk else set()
            for r, row in enumerate(self.data_b[t]):
                rewritable = set(pk_cols)
                refs: list[_ValidRef] = []
                for fk in ts.foreign_keys:
                    local_vals = [row[c] for c in fk.columns]
                    if any(v is None for v in local_vals):
                        # 非完整键（MATCH SIMPLE 部分 NULL / MATCH FULL 全 NULL）：
                        # 不是有效引用，相关单元格保持原值。
                        continue
                    ref_pk = self.schemas[fk.ref_table].pk
                    if ref_pk is None or set(fk.ref_columns) != set(ref_pk.columns):
                        # 资格校验（check_merge_eligible）已在之前拒绝；
                        # 这里是防御性二次检查。
                        raise MergeInconsistent(
                            f"外键 {t}.{fk.name} 未指向主键，无法合装"
                        )
                    wanted = dict(zip(fk.ref_columns, local_vals))
                    target = next(
                        (
                            s
                            for s, rrow in enumerate(self.data_b[fk.ref_table])
                            if all(rrow[c] == wanted[c] for c in ref_pk.columns)
                        ),
                        None,
                    )
                    if target is None:
                        raise MergeInconsistent(
                            f"{t} 第 {r + 1} 行的外键 {fk.name} 在第二份样本中"
                            "找不到被引用记录；已验证包不应出现该情况"
                        )
                    rewritable.update(fk.columns)
                    refs.append(
                        _ValidRef(
                            fk.name,
                            fk.ref_table,
                            target,
                            list(fk.columns),
                            list(fk.ref_columns),
                        )
                    )
                for c in sorted(rewritable):
                    # 可改写单元格必然非 NULL（主键非 NULL；有效引用键完整非 NULL）
                    self.vars[(t, r, c)] = z3.Int(f"mb__{t}__{r}__{c}")
                if refs:
                    self.valid_refs[(t, r)] = refs

    def _b_cell(self, t: str, r: int, c: str):
        """Second-sample cell as (is_statically_null, z3 expr or None)."""
        var = self.vars.get((t, r, c))
        if var is not None:
            return False, var
        val = self.data_b[t][r][c]
        if val is None:
            return True, None
        return False, z3.IntVal(val)

    # -------------------------------------------------------------- assertions
    def _labelled_assertions(
        self, probe_values: set[int]
    ) -> list[tuple[Group, z3.BoolRef]]:
        labelled: list[tuple[Group, z3.BoolRef]] = []
        labelled += self._bounds_assertions(probe_values)
        labelled += self._unique_assertions()
        labelled += self._reference_assertions()
        return labelled

    def _bounds_assertions(
        self, probe_values: set[int]
    ) -> list[tuple[Group, z3.BoolRef]]:
        out: list[tuple[Group, z3.BoolRef]] = []
        for (t, r, c), var in sorted(self.vars.items()):
            col = self.schemas[t].column(c)
            lo, hi = TYPE_BOUNDS[col.pg_type]
            g = self._group(
                f"bounds:{t}.{c}",
                f"整数类型边界：{t}.{c} 的改写值须落在 {col.pg_type} "
                f"范围 [{lo}, {hi}] 内",
                "range",
                [t, c],
            )
            out.append((g, z3.And(var >= lo, var <= hi)))
            # 验证器负向探测的哨兵值不得被改写后的键占用，否则试装时的
            # 悬空引用探测可能误中真实键而误报失败。
            for bad in probe_values:
                if lo <= bad <= hi:
                    out.append((g, var != bad))
        return out

    def _unique_assertions(self) -> list[tuple[Group, z3.BoolRef]]:
        out: list[tuple[Group, z3.BoolRef]] = []
        for t, ts in self.schemas.items():
            rows_a = self.data_a[t]
            n_a = len(rows_a)
            total = n_a + len(self.data_b[t])

            def cell(i: int, c: str):
                if i < n_a:
                    val = rows_a[i][c]
                    return (val is None, None if val is None else z3.IntVal(val))
                return self._b_cell(t, i - n_a, c)

            for uc in ts.unique_constraints:
                kind = "主键" if uc.is_primary else "唯一键"
                g = self._group(
                    f"unique:{t}.{uc.name}",
                    f"{kind}约束 {t}.{uc.name}（列: {', '.join(uc.columns)}）："
                    f"合装后全部 {total} 行的非 NULL 键两两不同",
                    "unique",
                    [t, uc.name],
                )
                for i in range(total):
                    for j in range(i + 1, total):
                        ci = [cell(i, c) for c in uc.columns]
                        cj = [cell(j, c) for c in uc.columns]
                        # SQL 唯一键语义：任一键列含 NULL 的行互不冲突。
                        # NULL 位置静态固定，因此这里可在编码期判定。
                        if any(n for n, _ in ci) or any(n for n, _ in cj):
                            continue
                        out.append(
                            (
                                g,
                                z3.Or(
                                    *[
                                        ei != ej
                                        for (_, ei), (_, ej) in zip(ci, cj)
                                    ]
                                ),
                            )
                        )
        return out

    def _reference_assertions(self) -> list[tuple[Group, z3.BoolRef]]:
        out: list[tuple[Group, z3.BoolRef]] = []
        for (t, r), refs in sorted(self.valid_refs.items()):
            for ref in refs:
                g = self._group(
                    f"fkref:{t}.{ref.fk_name}.{r}",
                    f"有效引用保持：{t} 第 {r + 1} 行经外键 {ref.fk_name} "
                    f"仍指向 {ref.ref_table} 第 {ref.ref_row + 1} 行那条记录的合并版",
                    "fk",
                    [t, ref.fk_name, ref.ref_table],
                )
                for lc, rc in zip(ref.local_cols, ref.ref_cols):
                    _, le = self._b_cell(t, r, lc)
                    _, re_ = self._b_cell(ref.ref_table, ref.ref_row, rc)
                    out.append((g, le == re_))
        return out

    # ------------------------------------------------------------------ solve
    def solve(self) -> dict[str, list[Row]]:
        # z3 is not thread-safe: serialize the whole solve across the process.
        with Z3_LOCK:
            return self._solve_locked()

    def _solve_locked(self) -> dict[str, list[Row]]:
        self._analyse()
        probe_values = _probe_sentinel_values(self.schemas)
        labelled = self._labelled_assertions(probe_values)

        opt = z3.Optimize()
        opt.set(timeout=SOLVER_TIMEOUT_MS)
        for _, con in labelled:
            opt.add(con)
        # 软偏好：第二份样本的可改写单元格尽量保留原值（仅撞号等必要时才改写）
        for (t, r, c), var in self.vars.items():
            opt.add_soft(var == self.data_b[t][r][c], weight=1)
        verdict = opt.check()
        if verdict == z3.unknown:
            raise MergeTimeout(
                "z3 在限定时间内无法判定合装可行性（unknown），不能据此断言无解"
            )
        if verdict == z3.unsat:
            raise MergeUnsatisfiable(self._unsat_core(labelled))
        return self._extract(opt.model())

    def _unsat_core(
        self, labelled: list[tuple[Group, z3.BoolRef]]
    ) -> list[Group]:
        per_group: dict[str, list[z3.BoolRef]] = {}
        group_by_key: dict[str, Group] = {}
        for g, con in labelled:
            per_group.setdefault(g.key, []).append(con)
            group_by_key[g.key] = g
        s = z3.Solver()
        s.set(timeout=SOLVER_TIMEOUT_MS)
        labels: dict[str, z3.BoolRef] = {}
        for key, cons in per_group.items():
            lab = z3.Bool(f"MCORE__{key.replace('.', '_').replace(':', '_')}")
            labels[key] = lab
            s.assert_and_track(z3.And(*cons), lab)
        if s.check() != z3.unsat:
            # 无法复现无解时保守报告全部约束组，而不是猜测。
            return list(group_by_key.values())
        core = {str(x) for x in s.unsat_core()}
        hit = {k for k, lab in labels.items() if str(lab) in core}
        if not hit:
            hit = set(group_by_key)
        minimal = self._minimize(per_group, hit)
        return [group_by_key[k] for k in minimal]

    @staticmethod
    def _minimize(
        per_group: dict[str, list[z3.BoolRef]], candidate: set[str]
    ) -> list[str]:
        remaining = list(candidate)
        essential: list[str] = []
        while remaining:
            key = remaining.pop(0)
            trial = set(essential) | set(remaining)
            s = z3.Solver()
            s.set(timeout=20_000)
            for k in trial:
                s.add(*per_group[k])
            verdict = s.check()
            if verdict == z3.unsat:
                # 去掉 key 仍无解：key 对解释冲突不是必需的。
                continue
            # sat -> key 是冲突所必需；unknown -> 保守保留。
            essential.append(key)
        return essential

    def _extract(self, model: z3.ModelRef) -> dict[str, list[Row]]:
        merged: dict[str, list[Row]] = {}
        for t in self.schemas:
            # 第一份样本全部值原样保留（拷贝，绝不共享/改动画源数据）。
            rows: list[Row] = [dict(r) for r in self.data_a[t]]
            for r, row in enumerate(self.data_b[t]):
                out: Row = {}
                for c, val in row.items():
                    var = self.vars.get((t, r, c))
                    out[c] = (
                        model.eval(var, model_completion=True).as_long()
                        if var is not None
                        else val
                    )
                rows.append(out)
            merged[t] = rows
        return merged

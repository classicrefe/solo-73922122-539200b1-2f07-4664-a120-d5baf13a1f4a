"""z3-based sample generation.

Every cell of every selected table is encoded as an integer value plus a
NULL indicator, so nullable semantics survive solving. All structural
constraints (PK/UNIQUE, composite MATCH SIMPLE/FULL foreign keys, including
self-references and cycles) are asserted as hard constraints; row counts and
the requested closed intervals are hard constraints as well.

Solving is staged:

1. prefer a sample in which every nullable cell is non-NULL;
2. if that is infeasible, re-solve allowing NULLs wherever the schema permits.

When no model exists, a labelled unsat core is mapped back to the specific
row count, range or constraint that participates in the conflict.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import z3

from .models import (
    Column,
    ForeignKey,
    TableSchema,
    UniqueConstraint,
)

NULL_MARKER = None
Row = dict[str, int | None]


@dataclass
class Cell:
    value: z3.ArithRef
    isnull: z3.BoolRef
    col: Column


@dataclass
class Group:
    key: str
    label: str
    kind: str  # rows | range | notnull | unique | fk
    refs: list[str] = field(default_factory=list)


class Unsatisfiable(Exception):
    def __init__(self, groups: list[Group]):
        self.groups = groups
        super().__init__("; ".join(g.label for g in groups))


class SolverTimeout(Exception):
    """Raised when z3 cannot decide within the configured budget.

    Distinct from Unsatisfiable: a timeout means "no answer in time", never
    "proven impossible", so the API must not claim a conflict.
    """


class Generator:
    def __init__(self, schemas: dict[str, TableSchema], specs: dict):
        self.schemas = schemas
        self.specs = specs  # table name -> TableSpec
        self.cells: dict[str, list[list[Cell]]] = {}
        self.groups: list[Group] = []
        self._group_keys: set[str] = set()

    # ------------------------------------------------------------------ utils
    def _group(self, key: str, label: str, kind: str, refs: list[str] | None = None) -> Group:
        if key in self._group_keys:
            return next(g for g in self.groups if g.key == key)
        g = Group(key=key, label=label, kind=kind, refs=refs or [])
        self._group_keys.add(key)
        self.groups.append(g)
        return g

    # --------------------------------------------------------------- encoding
    def _build_cells(self) -> None:
        for tname, ts in self.schemas.items():
            n = self.specs[tname].rows
            table_cells: list[list[Cell]] = []
            for r in range(n):
                row_cells: list[Cell] = []
                for col in ts.columns:
                    v = z3.Int(f"{tname}_{r}_{col.name}__v")
                    nu = z3.Bool(f"{tname}_{r}_{col.name}__null")
                    row_cells.append(Cell(value=v, isnull=nu, col=col))
                table_cells.append(row_cells)
            self.cells[tname] = table_cells

    def _cell_constraints(self, prefer_non_null: bool) -> list[z3.BoolRef]:
        soft: list[z3.BoolRef] = []
        for tname, ts in self.schemas.items():
            spec = self.specs[tname]
            rng_by_col = {c: spec.ranges[c] for c in spec.ranges}
            for r, row in enumerate(self.cells[tname]):
                for cell in row:
                    col = cell.col
                    rng = rng_by_col[col.name]
                    # Hard: NULL placement is only legal on nullable columns.
                    if not col.nullable:
                        yield cell.isnull == False  # noqa: E712
                    # Hard: non-NULL values must lie in the closed interval.
                    in_range = z3.And(cell.value >= rng.low, cell.value <= rng.high)
                    yield z3.Implies(cell.isnull == False, in_range)  # noqa: E712
                    if prefer_non_null:
                        soft.append(cell.isnull == False)  # noqa: E712
        yield from soft

    def _unique_constraint(
        self, tname: str, uc: UniqueConstraint
    ) -> z3.BoolRef:
        rows = self.cells[tname]
        idx = {c.name: i for i, c in enumerate(self.schemas[tname].columns)}
        terms = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                any_null = []
                value_diffs = []
                for cname in uc.columns:
                    a, b = rows[i][idx[cname]], rows[j][idx[cname]]
                    any_null.append(z3.Or(a.isnull, b.isnull))
                    value_diffs.append(a.value != b.value)
                # SQL UNIQUE semantics: rows containing a NULL in any key
                # column are distinct and never collide; only rows whose whole
                # constrained key is non-NULL must differ in at least one value.
                both_present = z3.Not(z3.Or(*any_null))
                terms.append(
                    z3.Implies(
                        both_present,
                        z3.Or(*value_diffs),
                    )
                )
        return z3.And(*terms) if terms else z3.BoolVal(True)

    def _fk_constraint(self, fk: ForeignKey, ts: TableSchema) -> z3.BoolRef:
        rows = self.cells[ts.name]
        ref_rows = self.cells[fk.ref_table]
        idx = {c.name: i for i, c in enumerate(ts.columns)}
        ref_ts = self.schemas[fk.ref_table]
        ridx = {c.name: i for i, c in enumerate(ref_ts.columns)}
        out = []
        for row in rows:
            local = [row[idx[c]] for c in fk.columns]
            n_local = len(local)
            any_null = z3.Or(*[c.isnull for c in local])
            all_null = z3.And(*[c.isnull for c in local])
            # Candidate match against every referenced row.
            matches = []
            for rr in ref_rows:
                ref_cells = [rr[ridx[c]] for c in fk.ref_columns]
                per_col = [
                    z3.And(
                        lc.isnull == False,  # noqa: E712
                        rc.isnull == False,  # noqa: E712
                        lc.value == rc.value,
                    )
                    for lc, rc in zip(local, ref_cells)
                ]
                matches.append(z3.And(*per_col))
            has_match = z3.Or(*matches) if matches else z3.BoolVal(False)
            if n_local == 1:
                # Single-column FK: a NULL local value is always allowed.
                out.append(z3.Implies(z3.Not(any_null), has_match))
            elif fk.match.value == "MATCH SIMPLE":
                # MATCH SIMPLE: if ANY local FK column is NULL (including a
                # mixed NULL/non-NULL key), the row is exempt from the match.
                # Only a fully non-NULL key must find a referenced row.
                out.append(z3.Implies(z3.Not(any_null), has_match))
            else:  # MATCH FULL
                # MATCH FULL: mixed NULL/non-NULL keys are forbidden; an
                # all-NULL key is allowed; otherwise a referenced row must match.
                out.append(z3.Implies(any_null, all_null))
                out.append(
                    z3.Or(
                        all_null,
                        z3.And(z3.Not(any_null), has_match),
                    )
                )
        return z3.And(*out) if out else z3.BoolVal(True)

    def _hard_constraints(self) -> list[tuple[Group, z3.BoolRef]]:
        labelled: list[tuple[Group, z3.BoolRef]] = []
        # Row count groups (the very existence of N cell rows encodes counts;
        # here we attach an explanatory label plus trivial TRUE marker).
        for tname, ts in self.schemas.items():
            n = self.specs[tname].rows
            g = self._group(
                f"rows:{tname}",
                f"行数约束：表 {tname} 恰好 {n} 行（0～20）",
                "rows",
                [tname],
            )
            labelled.append((g, z3.BoolVal(True)))
            # Range groups, one per column.
            for col in ts.columns:
                rng = self.specs[tname].ranges[col.name]
                grp = self._group(
                    f"range:{tname}.{col.name}",
                    f"区间约束：{tname}.{col.name} ∈ [{rng.low}, {rng.high}]"
                    + ("（NOT NULL，不可取 NULL）" if not col.nullable else "（可空，NULL 仅在可空列上允许）"),
                    "range",
                    [tname, col.name],
                )
                # Attach the actual range/nullability hard constraints here.
                for r, row in enumerate(self.cells[tname]):
                    cell = row[{c.name: i for i, c in enumerate(ts.columns)}[col.name]]
                    if not col.nullable:
                        labelled.append((grp, cell.isnull == False))  # noqa: E712
                    labelled.append(
                        (
                            grp,
                            z3.Implies(
                                cell.isnull == False,  # noqa: E712
                                z3.And(cell.value >= rng.low, cell.value <= rng.high),
                            ),
                        )
                    )
        # Unique constraints.
        for tname, ts in self.schemas.items():
            for uc in ts.unique_constraints:
                kind = "主键" if uc.is_primary else "唯一键"
                g = self._group(
                    f"unique:{tname}.{uc.name}",
                    f"{kind}约束 {tname}.{uc.name}（列: {', '.join(uc.columns)}）",
                    "unique",
                    [tname, uc.name],
                )
                labelled.append((g, self._unique_constraint(tname, uc)))
        # Foreign keys (cycles/self refs handled naturally by global solving).
        for tname, ts in self.schemas.items():
            for fk in ts.foreign_keys:
                g = self._group(
                    f"fk:{tname}.{fk.name}",
                    f"外键约束 {tname}.{fk.name}: ({', '.join(fk.columns)}) "
                    f"-> {fk.ref_table}({', '.join(fk.ref_columns)}) {fk.match.value}"
                    + ("（自引用/循环）" if fk.ref_table == tname else ""),
                    "fk",
                    [tname, fk.name, fk.ref_table],
                )
                labelled.append((g, self._fk_constraint(fk, ts)))
        return labelled

    # ------------------------------------------------------------------ solve
    def solve(self) -> dict[str, list[Row]]:
        self._build_cells()
        labelled = self._hard_constraints()

        # Stage 1: every nullable cell non-NULL (richest sample).
        result, timed_out = self._attempt(labelled, prefer_non_null=True)
        if result is not None:
            return result
        # Stage 1 may be infeasible purely because NULLs were disallowed.
        result, timed_out = self._attempt(labelled, prefer_non_null=False)
        if result is not None:
            return result
        if timed_out:
            # Neither attempt could decide in time; do not pretend it is unsat.
            raise SolverTimeout(
                "z3 在限定时间内无法判定可行性（unknown），不能据此断言无解；"
                "请缩小行数/区间或表数量后重试"
            )
        # Decided unsatisfiable: obtain and minimise an unsat core.
        core_groups = self._unsat_core(labelled)
        raise Unsatisfiable(core_groups)

    def _attempt(
        self, labelled: list[tuple[Group, z3.BoolRef]], prefer_non_null: bool
    ) -> tuple[dict[str, list[Row]] | None, bool]:
        s = z3.Solver()
        s.set(timeout=30000)
        # Merge constraints per group (same group object may repeat).
        per_group: dict[str, list[z3.BoolRef]] = {}
        group_by_key = {}
        for g, con in labelled:
            per_group.setdefault(g.key, []).append(con)
            group_by_key[g.key] = g
        labels: dict[str, z3.BoolRef] = {}
        for key, cons in per_group.items():
            lab = z3.Bool(f"LABEL__{key.replace('.', '_').replace(':', '_')}")
            labels[key] = lab
            s.add(z3.Implies(lab, z3.And(*cons)))
        if prefer_non_null:
            # Soft preference: try non-NULL everywhere first.
            for tname, rows in self.cells.items():
                for row in rows:
                    for cell in row:
                        if cell.col.nullable:
                            s.add(cell.isnull == False)  # noqa: E712
        check = s.check(*labels.values())
        if check == z3.unsat:
            return None, False
        if check == z3.unknown:
            # Could not decide in time; signal separately from a real unsat.
            return None, True
        model = s.model()
        return self._extract(model), False

    def _unsat_core(self, labelled: list[tuple[Group, z3.BoolRef]]) -> list[Group]:
        s = z3.Solver()
        s.set(timeout=60000)
        per_group: dict[str, list[z3.BoolRef]] = {}
        group_by_key = {}
        for g, con in labelled:
            per_group.setdefault(g.key, []).append(con)
            group_by_key[g.key] = g
        labels: dict[str, z3.BoolRef] = {}
        for key, cons in per_group.items():
            lab = z3.Bool(f"CORE__{key.replace('.', '_').replace(':', '_')}")
            labels[key] = lab
            s.assert_and_track(z3.And(*cons), lab)
        res = s.check(*labels.values())
        if res != z3.unsat:
            # Fallback: report every group as conflicting rather than guessing.
            return list(group_by_key.values())
        core = set(str(x) for x in s.unsat_core())
        hit = [group_by_key[k] for k, lab in labels.items() if str(lab) in core]
        if not hit:
            hit = list(group_by_key.values())
        # Minimise: greedily drop groups whose removal still leaves unsat.
        minimal = self._minimize(per_group, group_by_key, set(g.key for g in hit))
        return [group_by_key[k] for k in minimal]

    def _minimize(
        self,
        per_group: dict[str, list[z3.BoolRef]],
        group_by_key: dict[str, Group],
        candidate: set[str],
    ) -> list[str]:
        remaining = list(candidate)
        essential: list[str] = []
        while remaining:
            key = remaining.pop(0)
            trial = set(essential) | set(remaining)
            s = z3.Solver()
            s.set(timeout=20000)
            for k in trial:
                s.add(*per_group[k])
            verdict = s.check()
            if verdict == z3.unsat:
                # Removing key still leaves an infeasible system: key is not
                # needed to explain the conflict, drop it.
                continue
            # sat  -> key is essential to the conflict.
            # unknown -> cannot prove key redundant; keep it conservatively
            #            rather than silently dropping a real conflict.
            essential.append(key)
        return essential

    def _extract(self, model: z3.ModelRef) -> dict[str, list[Row]]:
        out: dict[str, list[Row]] = {}
        for tname, rows in self.cells.items():
            table_rows: list[Row] = []
            for row in rows:
                r: Row = {}
                for cell in row:
                    if z3.is_true(model.eval(cell.isnull, model_completion=True)):
                        r[cell.col.name] = NULL_MARKER
                    else:
                        r[cell.col.name] = model.eval(
                            cell.value, model_completion=True
                        ).as_long()
                table_rows.append(r)
            out[tname] = table_rows
        return out

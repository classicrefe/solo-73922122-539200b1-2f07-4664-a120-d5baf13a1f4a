"""Structural model of source-database tables.

Only catalog/metadata is represented here; row data is never read.
The model intentionally covers a restricted, explicitly-supported feature set:

* integer columns only (smallint / integer / bigint)
* primary keys (including composite)
* ordinary unique constraints (including composite)
* nullable / NOT NULL columns
* composite foreign keys with MATCH SIMPLE / MATCH FULL, including cycles

Everything else is represented as a rejection reason by the introspection
layer rather than being silently ignored.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

#: PostgreSQL integer type OIDs -> canonical name.
INT_TYPES: dict[int, str] = {
    21: "smallint",  # int2
    23: "integer",  # int4
    20: "bigint",  # int8
}

#: Hard value bounds per integer type (inclusive).
TYPE_BOUNDS: dict[str, tuple[int, int]] = {
    "smallint": (-32768, 32767),
    "integer": (-2147483648, 2147483647),
    "bigint": (-(2**63), 2**63 - 1),
}


class MatchType(str, Enum):
    SIMPLE = "MATCH SIMPLE"
    FULL = "MATCH FULL"


class Column(BaseModel):
    name: str
    pg_type: str
    ordinal: int
    nullable: bool
    has_default: bool
    is_identity: bool
    is_generated: bool  # ALWAYS stored generated column
    ord_in_pk: int | None = None


class UniqueConstraint(BaseModel):
    name: str
    columns: list[str]
    is_primary: bool


class ForeignKey(BaseModel):
    name: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    match: MatchType
    on_update: str
    on_delete: str
    not_deferrable: bool
    initially_deferred: bool


class TableSchema(BaseModel):
    schema_name: str
    name: str
    columns: list[Column]
    unique_constraints: list[UniqueConstraint]
    foreign_keys: list[ForeignKey]

    @property
    def full_name(self) -> str:
        return f"{self.schema_name}.{self.name}"

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def pk(self) -> UniqueConstraint | None:
        for u in self.unique_constraints:
            if u.is_primary:
                return u
        return None


class IntRange(BaseModel):
    """Closed integer interval requested for one column."""

    low: int
    high: int

    def validated(self, col: Column) -> "IntRange":
        if self.low > self.high:
            raise ValueError(
                f"列 {col.name} 的区间下界 {self.low} 大于上界 {self.high}"
            )
        bound_low, bound_high = TYPE_BOUNDS[col.pg_type]
        if self.low < bound_low or self.high > bound_high:
            raise ValueError(
                f"列 {col.name} 的区间 [{self.low}, {self.high}] 超出 "
                f"{col.pg_type} 类型边界 [{bound_low}, {bound_high}]"
            )
        return self


class TableSpec(BaseModel):
    """Per-table generation request."""

    table: str
    rows: int = Field(ge=0, le=20)
    ranges: dict[str, IntRange]


class GenerateRequest(BaseModel):
    schema_name: str = "public"
    tables: list[TableSpec]

    def table_names(self) -> list[str]:
        return [t.table for t in self.tables]


class Rejection(BaseModel):
    object: str
    reason: str


class JobStatus(str, Enum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ConflictReport(BaseModel):
    kind: str  # rows | range | constraint | structure | closure
    message: str
    details: dict = Field(default_factory=dict)


class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    conflict: ConflictReport | None = None
    log: list[str] = Field(default_factory=list)
    selected_tables: list[str] = Field(default_factory=list)

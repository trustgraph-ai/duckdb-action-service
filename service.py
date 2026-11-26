"""
DuckDB Action Service - executes structured queries against a DuckDB database.

A general-purpose TrustGraph remote action service that translates
QuerySpecArtifact queries into SQL and runs them against a DuckDB database.

Inputs:
    query: QuerySpecArtifact — a query specification with table,
    columns, filters, ordering, limit, group_by, and aggregations.

Returns:
    TabularArtifact with the query results.

Example remote-action config:
{
    "duckdb-query": "{
        \\"request-topic\\": \\"request:tg:duckdb-query\\",
        \\"response-topic\\": \\"response:tg:duckdb-query\\"
    }"
}
"""

import logging
import os

import duckdb

from trustgraph.base import RemoteActionService

logger = logging.getLogger(__name__)

default_ident = "duckdb-action-service"
default_topic = "duckdb-query"

FILTER_OPS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "contains": "LIKE",
    "in": "IN",
    "not_in": "NOT IN",
}

AGGREGATION_FUNCTIONS = {"sum", "count", "avg", "min", "max"}


def get_table_names(conn):
    result = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'"
    ).fetchall()
    return {row[0] for row in result}


def get_column_info(conn, table_name):
    result = conn.execute(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ?",
        [table_name],
    ).fetchall()
    return {row[0]: row[1] for row in result}


def validate_identifier(name, valid_set, kind="identifier"):
    if name not in valid_set:
        raise ValueError(
            f"Unknown {kind} '{name}'. "
            f"Available: {', '.join(sorted(valid_set))}"
        )


def build_filter_clause(col_expr, op, val, params):
    if op == "is_null":
        return f'{col_expr} IS NULL'
    elif op == "is_not_null":
        return f'{col_expr} IS NOT NULL'

    if op not in FILTER_OPS:
        raise ValueError(
            f"Unknown filter op '{op}'. "
            f"Supported: {', '.join(sorted(FILTER_OPS))}, is_null, is_not_null"
        )

    if val is None:
        if op == "eq":
            return f'{col_expr} IS NULL'
        elif op == "ne":
            return f'{col_expr} IS NOT NULL'

    sql_op = FILTER_OPS[op]

    if op == "contains":
        params.append(f"%{val}%")
        return f'{col_expr} {sql_op} ?'
    elif op in ("in", "not_in"):
        if not isinstance(val, list):
            val = [val]
        placeholders = ", ".join("?" for _ in val)
        params.extend(val)
        return f'{col_expr} {sql_op} ({placeholders})'
    else:
        params.append(val)
        return f'{col_expr} {sql_op} ?'


def build_select_sql(spec, table_names, conn):
    table_name = spec.get("table", "")
    validate_identifier(table_name, table_names, "table")

    column_info = get_column_info(conn, table_name)

    group_by = spec.get("group_by")
    aggregations = spec.get("aggregations")

    if aggregations and group_by:
        select_parts = []
        for col in group_by:
            validate_identifier(col, set(column_info), "column")
            select_parts.append(f'"{col}"')
        for agg in aggregations:
            func = agg["function"].lower()
            if func not in AGGREGATION_FUNCTIONS:
                raise ValueError(
                    f"Unknown aggregation '{func}'. "
                    f"Supported: {', '.join(sorted(AGGREGATION_FUNCTIONS))}"
                )
            col = agg["column"]
            validate_identifier(col, set(column_info), "column")
            alias = agg.get("alias", f"{func}_{col}")
            select_parts.append(f'{func}("{col}") AS "{alias}"')
        select_clause = ", ".join(select_parts)
    else:
        selected = spec.get("columns")
        if selected:
            for col in selected:
                validate_identifier(col, set(column_info), "column")
            select_clause = ", ".join(f'"{col}"' for col in selected)
        else:
            select_clause = "*"

    sql = f'SELECT {select_clause} FROM "{table_name}"'
    params = []

    filters = spec.get("filters", [])
    if filters:
        where_parts = []
        for f in filters:
            col = f["column"]
            validate_identifier(col, set(column_info), "column")
            clause = build_filter_clause(f'"{col}"', f["op"], f.get("value"), params)
            where_parts.append(clause)

        sql += " WHERE " + " AND ".join(where_parts)

    if aggregations and group_by:
        sql += " GROUP BY " + ", ".join(f'"{col}"' for col in group_by)

    ordering = spec.get("ordering")
    if ordering:
        order_parts = []
        for o in ordering:
            col = o["column"]
            validate_identifier(col, set(column_info), "column")
            direction = o.get("direction", "asc").upper()
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            order_parts.append(f'"{col}" {direction}')
        sql += " ORDER BY " + ", ".join(order_parts)

    limit = spec.get("limit")
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    return sql, params


def build_join_sql(spec, table_names, conn):
    left = spec["left"]
    right = spec["right"]

    left_table = left.get("table", "")
    right_table = right.get("table", "")
    validate_identifier(left_table, table_names, "table")
    validate_identifier(right_table, table_names, "table")

    left_cols = get_column_info(conn, left_table)
    right_cols = get_column_info(conn, right_table)

    on_clauses = spec.get("on", [])
    if not on_clauses:
        raise ValueError("Join spec must have at least one 'on' clause")

    for clause in on_clauses:
        validate_identifier(clause["left_column"], set(left_cols), "column")
        validate_identifier(clause["right_column"], set(right_cols), "column")

    group_by = spec.get("group_by")
    aggregations = spec.get("aggregations")

    def _qualify(col):
        if col in left_cols:
            return f'l."{col}"'
        return f'r."{col}"'

    if aggregations and group_by:
        select_parts = []
        all_cols = set(left_cols) | set(right_cols)
        for col in group_by:
            validate_identifier(col, all_cols, "column")
            select_parts.append(f'{_qualify(col)} AS "{col}"')
        for agg in aggregations:
            func = agg["function"].lower()
            if func not in AGGREGATION_FUNCTIONS:
                raise ValueError(
                    f"Unknown aggregation '{func}'. "
                    f"Supported: "
                    f"{', '.join(sorted(AGGREGATION_FUNCTIONS))}"
                )
            col = agg["column"]
            validate_identifier(col, all_cols, "column")
            alias = agg.get("alias", f"{func}_{col}")
            select_parts.append(f'{func}({_qualify(col)}) AS "{alias}"')
        select_clause = ", ".join(select_parts)
    else:
        overlap = set(left_cols) & set(right_cols)
        join_keys = {c["right_column"] for c in on_clauses}

        select_parts = [f'l."{col}"' for col in left_cols]
        for col in right_cols:
            if col in join_keys and col in overlap:
                continue
            if col in overlap:
                select_parts.append(
                    f'r."{col}" AS "{right_table}.{col}"'
                )
            else:
                select_parts.append(f'r."{col}"')
        select_clause = ", ".join(select_parts)

    join_conds = " AND ".join(
        f'l."{c["left_column"]}" = r."{c["right_column"]}"'
        for c in on_clauses
    )

    sql = (
        f"SELECT {select_clause} "
        f'FROM "{left_table}" l '
        f'JOIN "{right_table}" r ON {join_conds}'
    )
    params = []

    left_filters = left.get("filters", [])
    right_filters = right.get("filters", [])
    where_parts = []

    for f in left_filters:
        col = f["column"]
        validate_identifier(col, set(left_cols), "column")
        clause = build_filter_clause(f'l."{col}"', f["op"], f.get("value"), params)
        where_parts.append(clause)

    for f in right_filters:
        col = f["column"]
        validate_identifier(col, set(right_cols), "column")
        clause = build_filter_clause(f'r."{col}"', f["op"], f.get("value"), params)
        where_parts.append(clause)

    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    if aggregations and group_by:
        sql += " GROUP BY " + ", ".join(_qualify(col) for col in group_by)

    return sql, params


def type_name(duckdb_type):
    t = str(duckdb_type).upper()
    if t in ("INTEGER", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DOUBLE",
             "DECIMAL", "HUGEINT", "NUMERIC"):
        return "number"
    return "string"


class Processor(RemoteActionService):

    def __init__(self, **params):
        db_path = params.pop("db_path", None) or os.environ.get(
            "DUCKDB_PATH", "/data/duckdb.data"
        )
        read_only = params.pop("read_only", True)
        super(Processor, self).__init__(**params)

        self.conn = duckdb.connect(db_path, read_only=read_only)
        self.table_names = get_table_names(self.conn)

        logger.info(
            f"DuckDB action service initialized, "
            f"database: {db_path}, "
            f"tables: {', '.join(sorted(self.table_names))}"
        )

    async def execute(self, inputs, user_context):

        query_spec = inputs.get("query")

        if query_spec and isinstance(query_spec, dict):
            if "type" in query_spec:
                return self.execute_query_spec(query_spec, user_context)

        raise ValueError(
            "Missing 'query' (QuerySpecArtifact). "
            f"Available tables: {', '.join(sorted(self.table_names))}"
        )

    def execute_query_spec(self, spec, user_context):

        spec_type = spec.get("type")

        if spec_type == "select":
            sql, params = build_select_sql(
                spec, self.table_names, self.conn,
            )
        elif spec_type == "join":
            sql, params = build_join_sql(
                spec, self.table_names, self.conn,
            )
        else:
            raise ValueError(
                f"Unsupported query type '{spec_type}'. "
                f"Supported: select, join"
            )

        logger.info(
            f"Executing SQL: {sql} | params: {params} | "
            f"user={user_context.user_id if user_context else 'anonymous'}"
        )

        result = self.conn.execute(sql, params)
        columns = [
            {"name": desc[0], "type": type_name(desc[1])}
            for desc in result.description
        ]
        rows = [
            {desc[0]: val for desc, val in zip(result.description, row)}
            for row in result.fetchall()
        ]

        logger.info(f"Query returned {len(rows)} rows")

        return {
            "results": ("TabularArtifact", {
                "columns": columns, "rows": rows,
            }),
        }

    @staticmethod
    def add_args(parser):
        RemoteActionService.add_args(parser)

        parser.add_argument(
            "--db-path",
            default=os.environ.get("DUCKDB_PATH", "/data/duckdb.data"),
            help="Path to DuckDB database file (default: /data/duckdb.data)",
        )

        for action in parser._actions:
            if "--topic" in action.option_strings:
                action.default = default_topic
                break


def run():
    Processor.launch(default_ident, __doc__)


if __name__ == "__main__":
    run()

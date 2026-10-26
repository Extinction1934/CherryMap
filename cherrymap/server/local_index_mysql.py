"""Local MySQL endpoint for CherryMap's own GeoNames index.

The source-extension child may only open a managed MySQL URL. This process
speaks that protocol on 127.0.0.1 and stores the index in SQLite so coverage
clusters work without a separate database server. It is not the CherryPro
worker and it never downloads data.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp
from mysql_mimic.auth import NativePasswordAuthPlugin, SimpleIdentityProvider, User
from mysql_mimic.errors import MysqlError
from mysql_mimic.results import ResultColumn, ResultSet
from mysql_mimic.server import MysqlServer
from mysql_mimic.session import Session
from mysql_mimic.types import ColumnType

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "geonames.sqlite"
HOST = "127.0.0.1"
PORT = 3306
INDEX_PASSWORD = "local-index"


class LocalIndexIdentity(SimpleIdentityProvider):
    """Accept the local index user with a fixed password. Nothing else is exposed."""

    def __init__(self) -> None:
        self._auth_string = NativePasswordAuthPlugin.create_auth_string(INDEX_PASSWORD)

    async def get_user(self, username: str) -> User | None:
        if username != "cherrymap":
            return None
        return User(
            name=username,
            auth_string=self._auth_string,
            auth_plugin=NativePasswordAuthPlugin.name,
        )


def _column_type(value: object) -> ColumnType:
    if isinstance(value, bool):
        return ColumnType.LONGLONG
    if isinstance(value, int):
        return ColumnType.LONGLONG
    if isinstance(value, float):
        return ColumnType.DOUBLE
    return ColumnType.VAR_STRING


def _result(cursor: sqlite3.Cursor) -> ResultSet | None:
    if cursor.description is None:
        return None
    rows = cursor.fetchall()
    columns = []
    for index, column in enumerate(cursor.description):
        sample = next((row[index] for row in rows if row[index] is not None), None)
        columns.append(ResultColumn(column[0], _column_type(sample)))
    return ResultSet(rows, columns)


_UPSERT_KEYS = {
    "geo_imports": "dataset_key",
    "open_poi_places": "osm_key",
    "open_poi_covered_bboxes": "cache_key",
    "open_poi_migrations": "migration_key",
}


def _insert_table(expression: exp.Insert) -> str:
    target = expression.this
    if isinstance(target, exp.Schema):
        target = target.this
    if isinstance(target, exp.Table):
        return target.name
    return ""


def _upsert(expression: exp.Insert) -> str:
    conflict = expression.args.get("conflict")
    assignments: list[str] = []
    if conflict is not None:
        for node in conflict.find_all(exp.EQ):
            right = node.right
            if isinstance(right, exp.Anonymous) and right.name.upper() == "VALUES" and right.expressions:
                replacement = f"excluded.{right.expressions[0].sql(dialect='sqlite')}"
            else:
                replacement = right.sql(dialect="sqlite")
            assignments.append(f"{node.left.sql(dialect='sqlite')} = {replacement}")
        expression.set("conflict", None)
    statement = expression.sql(dialect="sqlite")
    if assignments:
        key = _UPSERT_KEYS.get(_insert_table(expression), "rowid")
        statement += f" ON CONFLICT({key}) DO UPDATE SET " + ", ".join(assignments)
    return statement


def _create(expression: exp.Create) -> list[str]:
    schema = expression.this
    if not isinstance(schema, exp.Schema):
        expression.set("properties", None)
        return [expression.sql(dialect="sqlite")]
    indexes: list[exp.IndexColumnConstraint] = []
    columns = []
    for part in list(schema.expressions):
        if isinstance(part, exp.IndexColumnConstraint):
            indexes.append(part)
        else:
            columns.append(part)
    schema.set("expressions", columns)
    expression.set("properties", None)
    table = schema.this.sql(dialect="sqlite")
    statements = [expression.sql(dialect="sqlite")]
    for index in indexes:
        name = index.this.sql(dialect="sqlite") if index.this is not None else ""
        fields = ", ".join(column.sql(dialect="sqlite") for column in index.expressions)
        if name and fields:
            statements.append(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({fields})")
    return statements


def to_sqlite(expression: exp.Expression) -> list[str]:
    if isinstance(expression, exp.Create):
        return _create(expression)
    if isinstance(expression, exp.Insert):
        return [_upsert(expression)]
    return [expression.sql(dialect="sqlite")]


class IndexSession(Session):
    def __init__(self, database: Path) -> None:
        super().__init__()
        self.db = sqlite3.connect(database, timeout=60, isolation_level="")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-65536")
        self.db.execute("PRAGMA busy_timeout=60000")

    async def _commit_middleware(self, q):  # type: ignore[override]
        if isinstance(q.expression, exp.Commit):
            self.db.commit()
            return [], []
        return await q.next()

    async def _rollback_middleware(self, q):  # type: ignore[override]
        if isinstance(q.expression, exp.Rollback):
            self.db.rollback()
            return [], []
        return await q.next()

    async def query(self, expression, sql, attrs):  # type: ignore[override]
        try:
            statements = to_sqlite(expression)
            cursor = None
            for statement in statements:
                cursor = self.db.execute(statement)
            if cursor is None:
                return None
            return _result(cursor)
        except sqlite3.Error as exc:
            raise MysqlError(f"index query failed: {exc}") from exc
        except sqlglot.errors.SqlglotError as exc:
            raise MysqlError("index query could not be prepared") from exc

    async def close(self) -> None:
        self.db.close()
        await super().close()


async def serve(database: Path = DB_PATH, host: str = HOST, port: int = PORT) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    server = MysqlServer(
        session_factory=lambda: IndexSession(database),
        identity_provider=LocalIndexIdentity(),
    )
    await server.serve_forever(host=host, port=port)


def main() -> int:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

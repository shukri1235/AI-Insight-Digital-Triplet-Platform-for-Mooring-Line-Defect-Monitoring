"""
pytest plugin: records every statement the app executes (both databases) and writes it as PostgreSQL SQL, so the
whole test-suite workload can be replayed on a real PostgreSQL server (tests/postgres/run_pg_compat.sh).

    FYP_CAPTURE_SQL=/tmp/workload.sql python -m pytest tests -p postgres.pg_capture
"""
import os
import threading
from datetime import datetime

_lock = threading.Lock()
_out = None
_stats = {"ok": 0, "unrenderable": 0}


def _literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ')}'"
    return "'" + str(v).replace("'", "''") + "'"


def _render(stmt, params, dialect):
    """The statement as PostgreSQL SQL with its values filled in (compiled by SQLAlchemy's PostgreSQL dialect)."""
    from sqlalchemy.sql.elements import TextClause
    if isinstance(stmt, TextClause):
        sql = stmt.text
        for k in sorted(params or {}, key=len, reverse=True):
            sql = sql.replace(f":{k}", _literal(params[k]))
        return sql
    from sqlalchemy.sql.ddl import ExecutableDDLElement
    if isinstance(stmt, ExecutableDDLElement):
        return str(stmt.compile(dialect=dialect))
    kw = {"compile_kwargs": {"render_postcompile": True}}       # expand IN (...) lists
    if params:
        kw["column_keys"] = list(params.keys())
    compiled = stmt.compile(dialect=dialect, **kw)
    values = compiled.construct_params(params or None)
    sql = compiled.string
    for k in sorted(values, key=len, reverse=True):
        sql = sql.replace(f"%({k})s", _literal(values[k]))
    return sql.replace("%%", "%")


def _listener(conn, clauseelement, multiparams, params, execution_options):
    from sqlalchemy.dialects import postgresql
    dialect = postgresql.dialect()
    if multiparams and isinstance(multiparams[0], (list, tuple)):
        plist = list(multiparams[0])
    elif multiparams:
        plist = list(multiparams)
    else:
        plist = [params or {}]
    lines = []
    for p in plist or [{}]:
        try:
            lines.append(_render(clauseelement, p, dialect).strip().rstrip(";") + ";")
            _stats["ok"] += 1
        except Exception as e:                                  # noqa: BLE001
            _stats["unrenderable"] += 1
            lines.append(f"-- UNRENDERABLE ({type(e).__name__}: {str(e)[:120]}): {str(clauseelement)[:200]!r}")
    with _lock:
        _out.write("\n".join(lines) + "\n")


def pytest_sessionstart(session):
    global _out
    path = os.environ.get("FYP_CAPTURE_SQL")
    if not path:
        return
    _out = open(path, "w", encoding="utf-8")
    from sqlalchemy import event
    import database
    import sensors_db
    for eng in (database.engine, sensors_db.engine):
        event.listen(eng, "before_execute", _listener)


def pytest_sessionfinish(session, exitstatus):
    if _out:
        _out.close()
        print(f"\n[pg_capture] {_stats['ok']} statements rendered for PostgreSQL, {_stats['unrenderable']} unrenderable")

"""
Copy the local SQLite databases into a database server (PostgreSQL / AWS RDS), once, when moving the app.

  pip install "psycopg[binary]"
  python tools/migrate_db.py --to "postgresql+psycopg://mooring:PASSWORD@my-db.xxxx.rds.amazonaws.com:5432/mooring"

Then point the app at the server (config.toml [database] url = "...", or FYP_DATABASE_URL) and restart it.

* Source: the SQLite files this app is configured with (paths.database and the sensor DB next to it). Stop the app
  first so nothing is written during the copy.
* Target: must be empty (an empty database, or one whose app tables are empty). The tables are created here with the
  app's own schema. --sensor-to puts the ROV sensor tables in a different database; by default they go to --to.
* Every row is copied in batches, the row counts are compared afterwards, and integer id sequences are moved past the
  copied ids so new rows do not collide. Media files (clips, key frames, .tlog) stay on disk - only the paths move.
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="SQLAlchemy URL of the target database")
    ap.add_argument("--sensor-to", help="URL for the ROV sensor tables (default: same as --to)")
    ap.add_argument("--batch", type=int, default=2000)
    a = ap.parse_args()

    for k in ("FYP_DATABASE_URL", "FYP_SENSOR_DATABASE_URL"):     # the source is always the local SQLite files
        os.environ.pop(k, None)
    os.environ["FYP_DATABASE_URL"] = ""
    os.environ["FYP_SENSOR_DATABASE_URL"] = ""
    from sqlalchemy import create_engine, func, inspect, insert, select, text
    import config
    import database
    import sensors_db
    if not database.IS_SQLITE:
        sys.exit("The source must be the local SQLite database (database.url is set in config.toml - remove it for the copy).")
    database.init_db()                                              # bring an old file up to the current schema
    sensors_db.init_sensor_db()

    jobs = [("inspection", database.engine, database.Base.metadata, a.to),
            ("sensors", sensors_db.engine, sensors_db.Base.metadata, a.sensor_to or a.to)]
    for label, src, meta, url in jobs:
        dst = create_engine(url)
        print(f"\n[{label}] {config.redact_url(str(src.url))}  ->  {config.redact_url(url)}")
        names = set(inspect(src).get_table_names())
        try:
            with dst.connect():
                pass
        except Exception as e:                                       # noqa: BLE001 - say what to fix, not a traceback
            msg = str(getattr(e, "orig", e)).strip().splitlines()[0]
            hint = ("wrong user name or password in the URL (special characters such as @ : / # % in a password must be "
                    "URL-encoded, e.g. @ -> %40)" if "password authentication failed" in msg else
                    "the database in the URL does not exist yet - create it first (CREATE DATABASE ...;)" if "does not exist" in msg else
                    "the server is not running or not reachable at that host and port" if "Connection refused" in msg or "timeout" in msg.lower() else
                    "install the driver: pip install \"psycopg[binary]\"" if "No module named" in msg else "see the message above")
            sys.exit(f"Cannot connect to {config.redact_url(url)}:\n  {msg}\n  -> {hint}")
        meta.create_all(dst)
        with dst.connect() as c:
            busy = [t.name for t in meta.sorted_tables if c.execute(select(func.count()).select_from(t)).scalar()]
        if busy:
            sys.exit(f"Target is not empty ({', '.join(busy)}). Use an empty database so nothing is mixed or duplicated.")
        for t in meta.sorted_tables:                                # parents before children (foreign keys)
            if t.name not in names:
                continue
            t0, n = time.time(), 0
            with src.connect() as s_conn, dst.begin() as d_conn:
                res = s_conn.execution_options(stream_results=True).execute(select(t))
                while True:
                    rows = res.fetchmany(a.batch)
                    if not rows:
                        break
                    d_conn.execute(insert(t), [dict(r._mapping) for r in rows])
                    n += len(rows)
            with src.connect() as s_conn, dst.connect() as d_conn:
                ns = s_conn.execute(select(func.count()).select_from(t)).scalar()
                nd = d_conn.execute(select(func.count()).select_from(t)).scalar()
            ok = "ok" if ns == nd else f"MISMATCH (source {ns})"
            print(f"  {t.name:<24} {nd:>9} rows  {time.time() - t0:5.1f} s  {ok}")
            if ns != nd:
                sys.exit("Row counts differ - the copy is incomplete; fix the error above and start again with an empty target.")
            if dst.dialect.name == "postgresql":                   # move id sequences past the copied ids
                for col in t.primary_key.columns:
                    if col.autoincrement is True or (col.autoincrement == "auto" and col.type.python_type is int):
                        with dst.begin() as d_conn:
                            d_conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', '{col.name}'), "
                                                f"COALESCE((SELECT MAX({col.name}) FROM {t.name}), 0) + 1, false)"))
    print("\nDone. Point the app at the server - config.toml:\n  [database]\n  url = \"<the --to URL>\"\n"
          "(or set FYP_DATABASE_URL), restart it, and keep the SQLite files as a backup.")


if __name__ == "__main__":
    main()

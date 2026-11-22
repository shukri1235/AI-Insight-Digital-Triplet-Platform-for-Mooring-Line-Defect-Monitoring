"""
Database schema for the dissertation (Figure 3.3), read from the app's own SQLite files. Standard library only.

  python tools\\schema_dump.py
  python tools\\schema_dump.py --db backend\\fyp_database.db --sensors backend\\fyp_sensors.db --out schema

Writes:
  d1_schema.txt   the CREATE TABLE / CREATE INDEX statements exactly as stored in the databases
  d1_schema.mmd   a Mermaid ER diagram: paste it into https://mermaid.live and export PNG / SVG
                  (solid line = foreign key the database enforces; dashed = id link kept by the app,
                  e.g. the sensor tables, which live in a separate database)

Start the app once first so both files exist. Reading does not change them.
"""
import argparse
import os
import sqlite3

# Links kept by the app, not declared as foreign keys (child table, column, parent table)
APP_LINKS = [
    ("videos", "parent_video_id", "videos", "clip of"),
    ("analysis_runs", "parent_run_id", "analysis_runs", "re-analysis of"),
    ("defects", "run_id", "analysis_runs", "found by"),
    ("reports", "scope_ref", "videos", "covers"),
    ("sensor_samples", "media_id", "videos", "telemetry of"),
    ("rov_events", "media_id", "videos", "events of"),
    ("object_detections", "media_id", "videos", "detections in"),
    ("object_detections", "run_id", "analysis_runs", "found by"),
]
TYPES = {"VARCHAR": "text", "TEXT": "text", "FLOAT": "real", "REAL": "real", "INTEGER": "int", "BOOLEAN": "bool",
         "DATETIME": "datetime"}


def tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(here, "backend", "fyp_database.db"))
    ap.add_argument("--sensors", default=None, help="default: fyp_sensors.db next to --db")
    ap.add_argument("--out", default="schema")
    ap.add_argument("--all-columns", action="store_true", help="Mermaid: every column (default: long tables trimmed)")
    ap.add_argument("--max-cols", type=int, default=14)
    ap.add_argument("--self-links", action="store_true", help="also draw clip -> dive and re-analysis -> run (busier)")
    a = ap.parse_args()
    sensors = a.sensors or os.path.join(os.path.dirname(os.path.abspath(a.db)), "fyp_sensors.db")
    for p in (a.db, sensors):
        if not os.path.exists(p):
            raise SystemExit(f"Not found: {p}  (start the app once, or give --db / --sensors)")
    os.makedirs(a.out, exist_ok=True)

    txt = ["Mooring Twin - database schema, read from the app's SQLite files", ""]
    mmd = ["erDiagram"]
    fks, cols_of = [], {}
    for path, title in ((a.db, "INSPECTION DATABASE"), (sensors, "ROV SENSOR DATABASE")):
        con = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
        txt += ["=" * 90, f"{title}  ({os.path.basename(path)})", "=" * 90]
        for t in tables(con):
            sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0]
            txt += ["", sql.strip() + ";"]
            txt += [r[0] + ";" for r in con.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (t,))]
            info = con.execute(f"PRAGMA table_info({t})").fetchall()
            tfks = {r[3]: r[2] for r in con.execute(f"PRAGMA foreign_key_list({t})")}
            fks += [(t, c, p) for c, p in tfks.items()]
            refs = {c for ch, c, _, _ in APP_LINKS if ch == t}
            cols_of[t] = len(info)
            keep = [r for r in info if a.all_columns or r[5] or r[1] in tfks or r[1] in refs] + \
                   [r for r in info if not (a.all_columns or r[5] or r[1] in tfks or r[1] in refs)]
            shown = keep if a.all_columns else keep[:a.max_cols]
            shown = sorted(shown, key=lambda r: r[0])            # table order
            mmd.append(f"    {t} {{")
            for cid, name, typ, notnull, dflt, pk in shown:
                key = "PK" if pk else "FK" if name in tfks else ""
                note = ' "id link"' if name in refs and not key else ""
                mmd.append(f"        {TYPES.get(typ.split('(')[0].upper(), typ.lower() or 'any')} {name} {key}{note}".rstrip())
            if len(shown) < len(info):
                mmd.append(f"        etc plus_{len(info) - len(shown)}_more_columns")
            mmd.append("    }")
        con.close()
    present = set(cols_of)
    for child, col, parent in fks:
        mmd.append(f'    {parent} ||--o{{ {child} : "{col}"')
    for child, col, parent, verb in APP_LINKS:
        if child in present and parent in present and (child != parent or a.self_links):
            mmd.append(f'    {parent} ||..o{{ {child} : "{col}"')
    txt += ["", "Declared foreign keys: " + ", ".join(f"{c}.{k} -> {p}" for c, k, p in fks),
            "Id links kept by the app (not declared): " + ", ".join(f"{c}.{k} -> {p}" for c, k, p, _ in APP_LINKS)]
    open(os.path.join(a.out, "d1_schema.txt"), "w", encoding="utf-8").write("\n".join(txt) + "\n")
    open(os.path.join(a.out, "d1_schema.mmd"), "w", encoding="utf-8").write("\n".join(mmd) + "\n")
    print("Tables: " + ", ".join(f"{t} ({n})" for t, n in cols_of.items()))
    print(f"Written: {os.path.join(a.out, 'd1_schema.txt')}, {os.path.join(a.out, 'd1_schema.mmd')}")
    print("Picture: open https://mermaid.live, paste d1_schema.mmd, then Actions > PNG (or SVG).")


if __name__ == "__main__":
    main()

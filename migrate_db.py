"""Copy every row from one database to another (for example your local SQLite file -> Supabase PostgreSQL).

    python migrate_db.py --to "postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres"
    python migrate_db.py --from sqlite:///data/platform.db --to <url>

The target tables are created if needed and must be EMPTY. Generated files (sheet PDFs, scans) are not part of the
database: copy the exams folder separately if you move servers.
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, text

from app import db as app_db


def normalise(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="src", default=f"sqlite:///{app_db.DB_PATH}")
    p.add_argument("--to", dest="dst", required=True)
    a = p.parse_args()
    src, dst = create_engine(normalise(a.src)), create_engine(normalise(a.dst))
    if src.url == dst.url:
        sys.exit("Source and target are the same database")
    app_db.Base.metadata.create_all(dst)
    tables = app_db.Base.metadata.sorted_tables               # parents before children
    with dst.begin() as out, src.connect() as inp:
        for t in tables:
            if out.execute(t.select().limit(1)).first():
                sys.exit(f"Target table '{t.name}' is not empty - refusing to copy into a used database")
        for t in tables:
            rows = [dict(r._mapping) for r in inp.execute(t.select())]
            if rows:
                out.execute(t.insert(), rows)
            print(f"{t.name}: {len(rows)} row(s)")
        if dst.dialect.name == "postgresql":                   # keep auto-numbering ahead of the copied ids
            for t in tables:
                if "id" in t.c and t.c.id.primary_key:
                    out.execute(text(f"SELECT setval(pg_get_serial_sequence('{t.name}', 'id'), COALESCE((SELECT MAX(id) FROM {t.name}), 1))"))
    print("Done.")


if __name__ == "__main__":
    main()

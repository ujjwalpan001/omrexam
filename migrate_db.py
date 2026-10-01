"""Copy every row from one database to another (for example your local SQLite file -> Supabase PostgreSQL).

    python migrate_db.py --to "postgresql://postgres.<ref>:<password>@<pooler-host>:5432/postgres"
    python migrate_db.py --from sqlite:///data/platform.db --to <url>
    python migrate_db.py --merge --teacher you@example.com                  (target = DATABASE_URL, already in use)

Without --merge the target tables are created if needed and must be EMPTY.
With --merge, exams (questions, sheets, results) and their students are ADDED to a database that is already in use:
students are matched by email or registration number, everything gets new ids, and an exam whose paper IDs are already
in the target is skipped, so running it twice is harmless. --teacher gives every copied exam to that existing teacher
account. Login sessions are not copied.

Generated files (sheet PDFs, scans) are not part of the database. The server rebuilds the files it needs to scan an
exam's sheets on its own; copy the exams folder separately if you also want the old PDFs and scan images.
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, func, select, text

from server import load_env

load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))      # DATABASE_URL is the default target
from app import db as app_db, transfer  # noqa: E402


def normalise(url: str) -> str:
    for prefix in ("postgres://", "postgresql://", "postgresql+psycopg://"):
        if url.startswith(prefix):                         # a password containing @ : / # ? still works
            return "postgresql+psycopg://" + app_db.encode_credentials(url[len(prefix):])
    return url


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="src", default=f"sqlite:///{app_db.DB_PATH}")
    p.add_argument("--to", dest="dst", default=os.environ.get("DATABASE_URL", ""), help="default: DATABASE_URL from .env")
    p.add_argument("--merge", action="store_true", help="add to a database that is already in use")
    p.add_argument("--teacher", default="", help="with --merge: the existing teacher account (email) that gets every copied exam")
    a = p.parse_args()
    if not a.dst:
        sys.exit("No target: pass --to <url> or set DATABASE_URL in .env")
    if a.merge and not a.teacher:
        sys.exit("--merge needs --teacher <the email you signed up with on the live site>")
    src, dst = create_engine(normalise(a.src)), create_engine(normalise(a.dst))
    if src.url == dst.url:
        sys.exit("Source and target are the same database")
    app_db.Base.metadata.create_all(dst)
    if a.merge:
        with dst.begin() as out, src.connect() as inp:
            owner = out.execute(select(app_db.User.id).where(func.lower(app_db.User.email) == a.teacher.lower(),
                                                             app_db.User.role == "teacher")).scalar()
            if owner is None:
                sys.exit(f"No teacher account with email {a.teacher} in the target - sign up on the site first")
            n = transfer.merge_data(out, transfer.export_data(inp), owner)
        print("\n".join(n["lines"]))
        print(f"{n['exams']} exam(s) copied, {n['skipped']} skipped; {n['students_added']} student(s) added. Done.")
        return
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

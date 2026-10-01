"""Save this computer's exams to a file you can import on the live site (Exams -> Import exams).

    python export_exams.py                    -> exams_export.json
    python export_exams.py --out my.json

Reads the local SQLite database. The file holds the exams, their questions,
sheets and results, and the students those results belong to (with their password hashes) - keep it private and never
commit it. Teachers and login sessions are not included: whoever imports the file owns the exams.
"""
import argparse
import os

from sqlalchemy import create_engine

from app import db as app_db, transfer


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="src", default=f"sqlite:///{app_db.DB_PATH}")
    p.add_argument("--out", default="exams_export.json")
    a = p.parse_args()
    with create_engine(a.src).connect() as conn:
        data = transfer.export_data(conn)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(transfer.dumps(data))
    for e in data["exams"]:
        print(f"  {e['header'].get('exam_title', '')}: {len(e['sheets'])} sheets, {len(e['results'])} results")
    print(f"{len(data['exams'])} exam(s) saved to {os.path.abspath(a.out)} - import it on the live site (Exams -> Import exams).")


if __name__ == "__main__":
    main()

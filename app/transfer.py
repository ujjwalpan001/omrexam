"""Move exams between databases: export them to a JSON file, merge that file into a database that is already in use.

The export holds the exams with their questions, sheets and results, plus the students those results belong to.
Teachers are not exported: whoever imports the file becomes the owner of every exam in it. Login sessions are left out."""
import datetime as dt
import json

from sqlalchemy import DateTime, func, select

from .db import Base

VERSION = 1
T = Base.metadata.tables
USERS, EXAMS, QUESTIONS, SHEETS, RESULTS = (T[n] for n in ("users", "exams", "exam_questions", "sheets", "results"))


def _plain(table, row) -> dict:
    """A row as JSON-ready values (datetimes as ISO text)."""
    out = dict(row)
    for c in table.c:
        if isinstance(c.type, DateTime) and out.get(c.name) is not None:
            out[c.name] = out[c.name].isoformat()
    return out


def _typed(table, row: dict) -> dict:
    """Back from JSON: ISO text -> datetime, and only the columns this table has."""
    out = {k: v for k, v in row.items() if k in table.c}
    for c in table.c:
        if isinstance(c.type, DateTime) and isinstance(out.get(c.name), str):
            out[c.name] = dt.datetime.fromisoformat(out[c.name])
    return out


def export_data(conn) -> dict:
    exams = []
    for e in conn.execute(EXAMS.select().order_by(EXAMS.c.id)).mappings():
        rows = lambda t: [_plain(t, r) for r in conn.execute(t.select().where(t.c.exam_id == e["id"]).order_by(t.c.id)).mappings()]
        exams.append(dict(_plain(EXAMS, e), questions=rows(QUESTIONS), sheets=rows(SHEETS), results=rows(RESULTS)))
    regs = {r["registration"] for e in exams for r in e["results"]}
    students = [_plain(USERS, u) for u in conn.execute(USERS.select().where(USERS.c.role == "student")).mappings()
                if u["reg_no"] in regs]
    return {"format": "omr-exams", "version": VERSION, "students": students, "exams": exams}


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def _insert(conn, table, row: dict, **changes) -> int:
    values = {k: v for k, v in dict(_typed(table, row), **changes).items() if k != "id"}
    return conn.execute(table.insert().values(**values).returning(table.c.id)).scalar_one()


def merge_data(conn, data: dict, owner_id: int) -> dict:
    """Add the exported exams to this database under teacher `owner_id`, with new ids. An exam whose paper IDs are already
    here is skipped (so importing the same file twice is harmless). Students are matched by email or registration number.
    The caller commits. Returns counts and one line per exam."""
    if data.get("format") != "omr-exams" or data.get("version") != VERSION:
        raise ValueError("This is not an exam export file")
    n = {"students_added": 0, "exams": 0, "skipped": 0, "questions": 0, "sheets": 0, "results": 0, "lines": []}
    for u in data["students"]:
        same = conn.execute(select(USERS.c.id).where(func.lower(USERS.c.email) == u["email"].lower())).scalar()
        if same is None and u.get("reg_no"):
            same = conn.execute(select(USERS.c.id).where(USERS.c.reg_no == u["reg_no"])).scalar()
        if same is None:
            _insert(conn, USERS, u, role="student")                # an edited file cannot create teacher accounts
            n["students_added"] += 1
    for e in data["exams"]:
        title = (e.get("header") or {}).get("exam_title", "")
        ids = [s["paper_id"] for s in e["sheets"]]
        if ids and conn.execute(select(func.count()).select_from(SHEETS).where(SHEETS.c.paper_id.in_(ids))).scalar():
            n["skipped"] += 1
            n["lines"].append(f"{title}: already here - skipped")
            continue
        new_exam = _insert(conn, EXAMS, e, teacher_id=owner_id)
        for q in e["questions"]:
            _insert(conn, QUESTIONS, q, exam_id=new_exam)
        sheet_ids = {s["id"]: _insert(conn, SHEETS, s, exam_id=new_exam, pdf_path="") for s in e["sheets"]}   # PDFs stay behind
        for r in e["results"]:
            _insert(conn, RESULTS, r, exam_id=new_exam, sheet_id=sheet_ids[r["sheet_id"]], image_path="")
        n["exams"] += 1
        n["questions"] += len(e["questions"])
        n["sheets"] += len(e["sheets"])
        n["results"] += len(e["results"])
        n["lines"].append(f"{title}: imported ({len(e['sheets'])} sheets)")
    return n

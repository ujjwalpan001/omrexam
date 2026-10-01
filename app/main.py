"""OMR platform API + static page.  Run:  python server.py  ->  http://localhost:8000"""
import csv
import datetime as dt
import io
import json
import shutil
import os
import random
import tempfile
import threading
import zipfile
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from omr import config, docscan, preprocess
from omr.pipeline import process_scan_full

from . import auth, results, sheets, textconv, transfer, workers
from .db import Exam, ExamQuestion, Result, ScanJob, Sheet, User, init_db

ALLOWED_COUNTS = sheets.bs.load_config()["allowed_counts"]      # 10, 15, 20, 25, 30
MAX_POOL = 1000
MAX_STUDENTS = 500
MAX_UPLOAD = 50 * 1024 * 1024
MAX_BATCH_UPLOAD = 300 * 1024 * 1024        # a scanned class set (many pages) can be large
TEACHER_INVITE = os.environ.get("OMR_TEACHER_INVITE", "")     # if set, teacher sign-up needs this code

app = FastAPI(title="OMR platform")
try:
    init_db()
except Exception as e:      # usually an unreachable / misconfigured DATABASE_URL
    raise SystemExit(f"Cannot open the database: {str(e).splitlines()[0][:200]}\nRun  python check_db.py  to diagnose DATABASE_URL, or leave it empty to use the local SQLite file.")


# ── schemas ────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=200)              # teachers only; students log in with their ID
    password: str = Field(min_length=1, max_length=200)
    role: str
    invite: str = ""
    reg_no: str = ""

    @field_validator("role")
    @classmethod
    def role_ok(cls, v):
        if v not in ("teacher", "student"):
            raise ValueError("role must be teacher or student")
        return v


class ReleaseIn(BaseModel):
    released: bool


class LoginIn(BaseModel):
    email: str            # email (teachers) or the 5-digit student ID
    password: str


class ExamIn(BaseModel):
    institution_name: str = Field(min_length=1, max_length=200)
    course_name: str = Field(min_length=1, max_length=200)
    faculty_name: str = Field(default="", max_length=200)
    exam_title: str = Field(min_length=1, max_length=200)
    test_number: str = Field(default="", max_length=50)
    total_marks: Optional[int] = Field(default=None, ge=0, le=10000)
    duration_mins: Optional[int] = Field(default=None, ge=0, le=1000)
    exam_date: Optional[dt.date] = None
    num_students: int = Field(default=1, ge=1, le=MAX_STUDENTS)
    shuffle_questions: bool = True
    shuffle_options: bool = False
    num_questions: int = 10                                    # questions printed on each sheet
    questions_text: str = Field(min_length=1, max_length=400_000)   # all questions pasted together

    @field_validator("num_questions")
    @classmethod
    def count_allowed(cls, v):
        if v not in ALLOWED_COUNTS:
            raise ValueError(f"number of questions per sheet must be one of {ALLOWED_COUNTS}")
        return v

    @model_validator(mode="after")
    def parse_questions(self):
        pool, errors = textconv.parse_bulk(self.questions_text)
        if errors:
            more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
            raise ValueError("; ".join(errors[:5]) + more)
        if len(pool) < self.num_questions:
            raise ValueError(f"found {len(pool)} question(s) but the sheet needs at least {self.num_questions}")
        if len(pool) > MAX_POOL:
            raise ValueError(f"too many questions ({len(pool)}); the limit is {MAX_POOL}")
        self._pool = pool
        return self

    _pool: list = PrivateAttr(default_factory=list)

    def header(self) -> dict:
        head = self.model_dump(include={"institution_name", "course_name", "faculty_name", "exam_title",
                                        "test_number", "total_marks", "duration_mins"})
        return dict(head, sheet_questions=self.num_questions, exam_date=self.exam_date.isoformat() if self.exam_date else None)

    def question_dicts(self) -> List[dict]:
        """The whole pasted question pool (each sheet uses `num_questions` of them)."""
        return self._pool


# ── auth ───────────────────────────────────────────────────────────────
REG_DIGITS = sheets.bs.load_config()["id_sections"]["registration"]["digits"]


def clean_reg_no(value: str) -> str:
    v = value.strip()
    if not (v.isdigit() and len(v) == REG_DIGITS):
        raise HTTPException(422, f"Registration number must be exactly {REG_DIGITS} digits (the digits you bubble on the sheet)")
    return v


def user_json(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role, "reg_no": u.reg_no}


@app.post("/api/register")
def register(body: RegisterIn, response: Response, db: Session = Depends(auth.get_db)):
    if body.role == "teacher" and TEACHER_INVITE and body.invite != TEACHER_INVITE:
        raise HTTPException(403, "Invalid teacher invite code")
    reg_no = clean_reg_no(body.reg_no) if body.role == "student" else None
    if body.role == "teacher" and (len(body.email.strip()) < 3 or len(body.password) < 8):
        raise HTTPException(422, "Teachers need an email and a password of at least 8 characters")
    email = body.email.strip().lower() if body.role == "teacher" else f"{reg_no}@student.local"      # students log in with their ID
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(409, "Email already registered")
    if reg_no and db.query(User).filter_by(reg_no=reg_no).first():
        raise HTTPException(409, "That student ID is already taken")
    user = User(email=email, name=body.name.strip(), role=body.role, password_hash=auth.hash_password(body.password), reg_no=reg_no)
    db.add(user)
    db.commit()
    response.set_cookie(auth.COOKIE, auth.create_session(db, user), httponly=True, samesite="lax")
    return user_json(user)


@app.post("/api/login")
def login(body: LoginIn, response: Response, db: Session = Depends(auth.get_db)):
    ident = body.email.strip().lower()
    user = db.query(User).filter_by(reg_no=ident, role="student").first() if ident.isdigit() else db.query(User).filter_by(email=ident).first()
    if not user or not auth.check_password(body.password, user.password_hash):
        raise HTTPException(401, "Wrong email or password")
    response.set_cookie(auth.COOKIE, auth.create_session(db, user), httponly=True, samesite="lax")
    return user_json(user)


@app.post("/api/logout")
def logout(request: Request, response: Response, db: Session = Depends(auth.get_db)):
    token = request.cookies.get(auth.COOKIE)
    if token:
        auth.drop_session(db, token)
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(auth.current_user)):
    return user_json(user)


# ── exams (teacher) ────────────────────────────────────────────────────
def own_exam(db: Session, exam_id: int, user: User) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None or exam.teacher_id != user.id:
        raise HTTPException(404, "Exam not found")
    return exam


def exam_json(e: Exam, detail: bool = False, scanned: int = 0) -> dict:
    out = {"id": e.id, "title": e.header["exam_title"], "course": e.header["course_name"],
           "test_number": e.header["test_number"], "num_students": e.num_students, "status": e.status,
           "progress": e.progress, "error": e.error, "created": e.created.isoformat(), "exam_date": e.header.get("exam_date"),
           "num_questions": e.header.get("sheet_questions", len(e.questions)), "pool_size": len(e.questions),
           "marks_released": e.marks_released, "scanned": scanned}
    if detail:
        out["sheets"] = [{"student_no": s.student_no, "paper_id": s.paper_id, "qr": sheets.qr_link(s.qr_token)} for s in e.sheets]
    return out


@app.post("/api/exams/preview")
def preview(body: ExamIn, user: User = Depends(auth.teacher_only)):
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "preview.pdf")
        try:
            sheets.preview_pdf(body.header(), body.question_dicts(), body.num_questions, pdf)
        except (sheets.CompileError, sheets.bs.SheetError) as e:
            raise HTTPException(422, str(e))
        with open(pdf, "rb") as f:
            return Response(f.read(), media_type="application/pdf")


@app.post("/api/exams")
def create_exam(body: ExamIn, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    try:  # fail fast on layout problems (e.g. too many questions) before creating anything
        sheets.ExamSheetBuilder(body.num_questions)
    except sheets.bs.SheetError as e:
        raise HTTPException(422, str(e))
    used = {p for (p,) in db.query(Sheet.paper_id)}
    rng, ids = random.SystemRandom(), []
    while len(ids) < body.num_students:                 # random unused 6-digit paper IDs (100000-999999)
        pid = str(rng.randrange(100000, 1000000))
        if pid not in used:
            used.add(pid)
            ids.append(pid)

    import secrets
    exam = Exam(teacher_id=user.id, header=body.header(), num_students=body.num_students,
                shuffle_questions=body.shuffle_questions, shuffle_options=body.shuffle_options)
    exam.questions = [ExamQuestion(position=i, text=q["text"], options=q["options"], correct=q["correct"])
                      for i, q in enumerate(body.question_dicts())]
    exam.sheets = [Sheet(student_no=i + 1, paper_id=pid, qr_token=secrets.token_hex(12)) for i, pid in enumerate(ids)]
    db.add(exam)
    db.commit()
    threading.Thread(target=sheets.generate_exam, args=(exam.id,), daemon=True).start()
    return exam_json(exam)


@app.get("/api/exams")
def list_exams(user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    rows = db.query(Exam).filter_by(teacher_id=user.id).order_by(Exam.id.desc()).all()
    counts = dict(db.query(Result.exam_id, func.count(Result.id)).filter(Result.exam_id.in_([e.id for e in rows])).group_by(Result.exam_id).all()) if rows else {}
    return [exam_json(e, scanned=counts.get(e.id, 0)) for e in rows]


@app.get("/api/exams/{exam_id}")
def get_exam(exam_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    exam = own_exam(db, exam_id, user)
    return exam_json(exam, detail=True, scanned=db.query(Result).filter_by(exam_id=exam.id).count())


@app.get("/api/exams/{exam_id}/sheets/{paper_id}.pdf")
def sheet_pdf(exam_id: int, paper_id: str, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    exam = own_exam(db, exam_id, user)
    s = next((s for s in exam.sheets if s.paper_id == paper_id), None)
    if s is None or not s.pdf_path or not os.path.exists(s.pdf_path):
        raise HTTPException(404, "Sheet not ready")
    return FileResponse(s.pdf_path, media_type="application/pdf")


@app.get("/api/exams/{exam_id}/download.zip")
def download_zip(exam_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    exam = own_exam(db, exam_id, user)
    if exam.status != "ready":
        raise HTTPException(409, "Sheets are still being generated")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in exam.sheets:
            z.write(s.pdf_path, f"students/sheet_{s.student_no:03d}_{s.paper_id}.pdf")
        z.write(os.path.join(sheets.exam_dir(exam_id), "all_sheets.pdf"), "PRINT_ALL.pdf")
        z.writestr("manifest.csv", sheets.manifest_csv(exam))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="exam_{exam.id}_sheets.zip"'})


@app.get("/api/exams/{exam_id}/print.pdf")
def print_all(exam_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """Every student's sheet combined in one PDF, in student order."""
    exam = own_exam(db, exam_id, user)
    path = os.path.join(sheets.exam_dir(exam_id), "all_sheets.pdf")
    if exam.status != "ready" or not os.path.exists(path):
        raise HTTPException(409, "Sheets are still being generated")
    return FileResponse(path, media_type="application/pdf", filename=f"exam_{exam_id}_all_sheets.pdf")


@app.get("/api/exams/{exam_id}/manifest.csv")
def manifest(exam_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    return PlainTextResponse(sheets.manifest_csv(own_exam(db, exam_id, user)), media_type="text/csv")


def grade_file(db: Session, exam: Exam, path: str, name: str, replace: bool = False) -> dict:
    """Grade one image (or first PDF page) of this exam's sheet and store the result. Raises on unreadable input.

    A sheet the teacher has already checked is skipped (res["skipped"]) unless `replace` is set; a student's self-check
    is always replaced by the teacher's scan."""
    d = sheets.scan_dir(exam)
    res, warped, fills = process_scan_full(path, os.path.join(d, "keys.json"), os.path.join(d, "layout.json"),
                                           os.path.join(d, "baseline.json"))
    clean_page = docscan.clean_page(warped)                 # the page the grader already straightened
    res["file"] = name
    sheet = next((s for s in exam.sheets if s.paper_id == res["paper_id"]), None)
    res["saved"] = False
    if sheet is None:
        res["flags"].append("Paper ID not recognised for this exam - result not saved")
        return res
    existing = db.query(Result).filter_by(sheet_id=sheet.id).first()
    if existing and existing.source == "teacher" and not replace:
        res.update(skipped=True, existing={"result_id": existing.id, "registration": existing.registration, "score": existing.score,
                                          "total": existing.total, "needs_review": existing.needs_review,
                                          "scanned_at": existing.created.isoformat()})
        return res
    row = save_result(db, exam, sheet, res, warped, fills, clean_page, source="teacher")
    student = db.query(User).filter_by(reg_no=res["registration"], role="student").first() if res["registration"] != "INVALID" else None
    statuses = [results.status_of(res["answers"].get(str(i + 1), "BLANK"), sheet.key[i]) for i in range(len(sheet.key))]
    res.update(saved=True, result_id=row.id, student=student.name if student else None, key=sheet.key,
               counts={k: statuses.count(k) for k in ("correct", "wrong", "blank", "invalid")})
    return res


@app.post("/api/exams/{exam_id}/scan")
async def scan_sheet(exam_id: int, file: UploadFile = File(...), user: User = Depends(auth.teacher_only),
                     db: Session = Depends(auth.get_db)):
    """Grade one scanned answer sheet (PDF or image) of this exam."""
    exam = own_exam(db, exam_id, user)
    if exam.status != "ready":
        raise HTTPException(409, "Sheets are not generated yet")
    data = await file.read()
    if not 0 < len(data) <= MAX_UPLOAD:
        raise HTTPException(400, "Empty or too large file")
    name = os.path.basename(file.filename or "scan.pdf")

    def work():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "upload" + (os.path.splitext(name)[1].lower() or ".pdf"))
            with open(path, "wb") as f:
                f.write(data)
            try:
                return grade_file(db, exam, path, name)
            except Exception as e:
                raise HTTPException(422, str(e))
    return await workers.run(work)                  # waits its turn: only a few scans run at once


# ── CSV exports ────────────────────────────────────────────────────────
def csv_safe(value):
    """Stop spreadsheet programs from running text such as '=cmd(...)' that a student typed as their name."""
    return "'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") else value


def pct_of(score, total) -> str:
    return f"{100 * score / total:.1f}" if total else ""


MARK_WORDS = {"correct": "right", "wrong": "wrong", "blank": "missed", "invalid": "multiple"}


def marked_cell(mine: str, correct: str) -> str:
    """What the student marked, with the verdict in brackets: 'B (right)', 'A (wrong)', '- (missed)', '- (multiple)'."""
    status = results.status_of(mine, correct)
    if mine == "UNCERTAIN":
        return "- (unclear)"
    letter = mine if status in ("correct", "wrong") else "-"
    return f"{letter} ({MARK_WORDS[status]})"


def question_cells(answers, key: list, n: int) -> list:
    """Two cells per question: the correct answer, then what the student marked. Empty marked cell = not scanned."""
    cells = []
    for i in range(1, n + 1):
        cells.append(key[i - 1] if i <= len(key) else "")
        cells.append("" if answers is None else marked_cell(answers.get(str(i), "BLANK"), key[i - 1]))
    return cells


def question_headers(n: int) -> list:
    return [h for i in range(1, n + 1) for h in (f"Q{i}_correct", f"Q{i}_marked")]


def exam_results_csv(db: Session, exam: Exam) -> str:
    """One row per printed sheet - checked or not - with the student's details, marks and every answer marked right/wrong."""
    n = exam.header.get("sheet_questions", len(exam.questions))
    results_by_sheet = {r.sheet_id: r for r in db.query(Result).filter_by(exam_id=exam.id)}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["student_no", "paper_id", "registration_no", "student_name", "student_login_id", "status", "score", "total",
                "percentage", "max_marks", "correct", "wrong", "not_answered", "unclear", "needs_review", "review_status", "flags", "source",
                "scanned_at", "institution", "subject", "exam", "exam_date"] + question_headers(n))
    h = exam.header
    for s in exam.sheets:
        r = results_by_sheet.get(s.id)
        student = db.query(User).filter_by(reg_no=r.registration, role="student").first() if r else None
        counts = ["", "", "", ""]
        if r:
            st = [results.status_of(r.answers.get(str(i), "BLANK"), s.key[i - 1]) for i in range(1, n + 1)]
            counts = [st.count("correct"), st.count("wrong"), st.count("blank"), st.count("invalid")]
        row = [s.student_no, s.paper_id, r.registration if r else "", csv_safe(student.name) if student else "",
               student.reg_no if student else "", "checked" if r else "not scanned",
               r.score if r else "", r.total if r else "", pct_of(r.score, r.total) if r else "", h.get("total_marks") or ""] + counts + [
               ("yes" if r.needs_review and not r.resolved else "no") if r else "", results.review_state(r).replace("review", "needs review") if r else "",
               csv_safe("; ".join(r.flags)) if r else "", r.source if r else "", r.created.isoformat(timespec="seconds") if r else "",
               csv_safe(h["institution_name"]), csv_safe(h["course_name"]), csv_safe(h["exam_title"]), h.get("exam_date") or ""]
        row += question_cells(r.answers if r else None, s.key, n)
        w.writerow(row)
    return buf.getvalue()


def batch_csv(items: list) -> str:
    """One row per scanned page of an upload, including the pages that could not be read; saved pages show every
    answer with the correct one and right/wrong."""
    n = max([len(it.get("key", [])) for it in items] + [0])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["page", "status", "reason", "paper_id", "registration_no", "student_name", "score", "total", "percentage",
                "correct", "wrong", "not_answered", "unclear", "needs_review", "flags"] + question_headers(n))
    for it in items:
        ok = it.get("ok")
        c = it.get("counts", {})
        skipped = it.get("skipped")
        row = [csv_safe(it["page"]), "saved" if ok else "skipped (already processed)" if skipped else "NOT SAVED",
               "" if ok else csv_safe(it.get("error", "")),
               it.get("paper_id", ""), it.get("registration", ""), csv_safe(it.get("student") or ""),
               it.get("score", "") if ok or skipped else "", it.get("total", "") if ok or skipped else "",
               pct_of(it["score"], it["total"]) if (ok or skipped) and it.get("total") else "",
               c.get("correct", ""), c.get("wrong", ""), c.get("blank", ""), c.get("invalid", ""),
               ("yes" if it.get("needs_review") else "no") if ok else "", csv_safe("; ".join(it.get("flags", []))) if ok else ""]
        row += question_cells(it["answers"], it["key"], n) if ok and it.get("key") else [""] * (2 * n)
        w.writerow(row)
    return buf.getvalue()


def save_csv_files(db: Session, exam: Exam, job_id: str, items: list) -> None:
    """Keep a copy on disk: the whole exam's results.csv and this batch's CSV."""
    d = sheets.exam_dir(exam.id)
    os.makedirs(os.path.join(d, "batches"), exist_ok=True)
    with open(os.path.join(d, "results.csv"), "w", newline="") as f:
        f.write(exam_results_csv(db, exam))
    with open(os.path.join(d, "batches", f"{job_id}.csv"), "w", newline="") as f:
        f.write(batch_csv(items))


# ── batch grading: many pages / one big PDF at once ───────────────────
JOBS: dict = {}                       # job id -> progress (in memory; lost on restart)
MAX_BATCH_FILES = 300


def run_batch(job_id: str, exam_id: int, uploads: List[tuple], tmp, replace: bool = False) -> None:
    """Background worker. uploads = [(saved path, original name)]."""
    from .db import SessionLocal
    job, db = JOBS[job_id], SessionLocal()
    try:
        exam = db.get(Exam, exam_id)
        for path, name in uploads:
            try:
                with workers.slot():
                    pages = preprocess.split_pages(path, tmp.name)
            except Exception as e:
                job["items"].append({"page": name, "ok": False, "error": f"Could not open file: {e}"})
                job["done"] += 1
                continue
            for i, page_path in enumerate(pages):
                label = name if len(pages) == 1 else f"{name} - page {i + 1}"
                try:
                    with workers.slot():                    # one page at a time, so student uploads can get in between
                        r = grade_file(db, exam, page_path, label, replace)
                    if r.get("skipped"):
                        ex = r["existing"]
                        job["items"].append({"page": label, "ok": False, "skipped": True, "error": "Already processed",
                                             "paper_id": r["paper_id"], "registration": ex["registration"], "score": ex["score"],
                                             "total": ex["total"], "needs_review": ex["needs_review"], "flags": []})
                        job["done"] += 1
                        continue
                    ok = r["saved"]
                    job["items"].append({"page": label, "ok": ok, "error": "" if ok else "; ".join(r["flags"][-1:]),
                                         "paper_id": r["paper_id"], "registration": r["registration"], "score": r["score"],
                                         "total": r["total"], "needs_review": r["needs_review"], "flags": r["flags"],
                                         "student": r.get("student"), "answers": r["answers"], "key": r.get("key", []),
                                         "counts": r.get("counts", {})})
                except Exception as e:
                    job["items"].append({"page": label, "ok": False, "error": str(e)})
                job["done"] += 1
    finally:
        try:
            save_csv_files(db, db.get(Exam, exam_id), job_id, job["items"])
        except Exception:
            pass                                  # the CSV can still be downloaded on demand
        job["status"] = "finished"
        db.close()
        tmp.cleanup()


@app.post("/api/exams/{exam_id}/scan_batch")
async def scan_batch(exam_id: int, files: List[UploadFile] = File(...), replace: bool = Form(False), user: User = Depends(auth.teacher_only),
                     db: Session = Depends(auth.get_db)):
    """Grade many pages at once: any mix of images and PDFs (one student sheet per page). Runs in the background."""
    import uuid
    exam = own_exam(db, exam_id, user)
    if exam.status != "ready":
        raise HTTPException(409, "Sheets are not generated yet")
    if not 0 < len(files) <= MAX_BATCH_FILES:
        raise HTTPException(400, f"Send between 1 and {MAX_BATCH_FILES} files")
    tmp = tempfile.TemporaryDirectory()
    uploads, total = [], 0
    for i, up in enumerate(files):
        data = await up.read()
        if not 0 < len(data) <= MAX_BATCH_UPLOAD:
            tmp.cleanup()
            raise HTTPException(400, f"{up.filename}: empty or too large")
        name = os.path.basename(up.filename or f"page{i + 1}")
        path = os.path.join(tmp.name, f"{i:03d}_" + name.replace(os.sep, "_"))
        with open(path, "wb") as f:
            f.write(data)
        try:
            total += preprocess.page_count(path)
        except Exception:
            total += 1
        uploads.append((path, name))
    job_id = uuid.uuid4().hex[:12]
    for old in list(JOBS)[:-50]:
        JOBS.pop(old, None)
    JOBS[job_id] = {"exam_id": exam_id, "teacher_id": user.id, "status": "running", "total": total, "done": 0, "items": []}
    threading.Thread(target=run_batch, args=(job_id, exam_id, uploads, tmp, replace), daemon=True).start()
    return {"job": job_id, "total": total}


@app.get("/api/exams/{exam_id}/scan_batch/{job_id}")
def scan_batch_status(exam_id: int, job_id: str, user: User = Depends(auth.teacher_only)):
    job = JOBS.get(job_id)
    if not job or job["exam_id"] != exam_id or job["teacher_id"] != user.id:
        raise HTTPException(404, "Job not found")
    return {k: job[k] for k in ("status", "total", "done", "items")}


def teacher_result(db: Session, exam_id: int, result_id: int, user: User) -> tuple:
    exam = own_exam(db, exam_id, user)
    r = db.get(Result, result_id)
    if r is None or r.exam_id != exam.id:
        raise HTTPException(404, "Result not found")
    return exam, r, db.get(Sheet, r.sheet_id)


def teacher_detail(db: Session, exam: Exam, r: Result, sheet: Sheet) -> dict:
    items = results.review_items(exam, sheet, r.answers, r.original_answers)
    counts = {k: sum(1 for i in items if i["status"] == k) for k in ("correct", "wrong", "blank", "invalid")}
    student = db.query(User).filter_by(reg_no=r.registration, role="student").first()
    return dict(result_summary(r, exam, sheet), questions=items, counts=counts, student=student.name if student else None,
                flagged=bool(r.needs_review), edited_at=r.edited_at.isoformat() if r.edited_at else None,
                image_version=int(r.edited_at.timestamp()) if r.edited_at else 0)


class AnswerEditIn(BaseModel):
    question: int = Field(ge=1, le=100)
    answer: str                                   # A-D, BLANK, or ORIGINAL (undo: go back to what the scanner read)

    @field_validator("answer")
    @classmethod
    def answer_ok(cls, v):
        v = v.strip().upper()
        if v not in ("A", "B", "C", "D", "BLANK", "ORIGINAL"):
            raise ValueError("answer must be A, B, C, D, BLANK or ORIGINAL")
        return v


class ReviewIn(BaseModel):
    resolved: bool


def only_question_flags(flags: list) -> bool:
    """True when every review flag is about a question (those can be fixed by correcting answers)."""
    return all(f.startswith("Q") for f in flags)


@app.patch("/api/exams/{exam_id}/results/{result_id}/answers")
def edit_answer(exam_id: int, result_id: int, body: AnswerEditIn, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """Correct one answer of a checked sheet (for example a double mark the scanner could not decide). The score is
    recalculated, the marked image is redrawn, and the review is marked resolved once no unclear answer is left."""
    exam, r, sheet = teacher_result(db, exam_id, result_id, user)
    if body.question > len(sheet.key):
        raise HTTPException(422, f"This sheet has {len(sheet.key)} questions")
    q = str(body.question)
    answers, original = dict(r.answers), dict(r.original_answers or {})
    if body.answer == "ORIGINAL":
        if q not in original:
            raise HTTPException(409, "This answer has not been changed")
        answers[q] = original.pop(q)
    else:
        original.setdefault(q, answers.get(q, "BLANK"))
        answers[q] = body.answer
        if original[q] == answers[q]:                      # corrected back to exactly what was read
            original.pop(q)
    r.answers, r.original_answers = answers, original or None
    r.score = results.rescore(answers, sheet.key)
    r.edited_at = dt.datetime.now(dt.timezone.utc)
    unclear_left = any(answers.get(str(i + 1)) in ("MULTIPLE", "UNCERTAIN") for i in range(len(sheet.key)))
    if r.needs_review:
        r.resolved = (not unclear_left) and only_question_flags(r.flags)
    layout = json.load(open(os.path.join(sheets.scan_dir(exam), "layout.json")))
    redrawn = results.rerender(r.image_path, layout, answers, sheet.key)
    db.commit()
    return dict(teacher_detail(db, exam, r, sheet), image_redrawn=redrawn)


@app.patch("/api/exams/{exam_id}/results/{result_id}/review")
def set_review(exam_id: int, result_id: int, body: ReviewIn, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """Toggle between 'needs review' and 'resolved' for a flagged sheet."""
    exam, r, sheet = teacher_result(db, exam_id, result_id, user)
    if not r.needs_review:
        raise HTTPException(409, "This sheet has nothing flagged")
    r.resolved = body.resolved
    db.commit()
    return teacher_detail(db, exam, r, sheet)


@app.get("/api/exams/{exam_id}/results/{result_id}")
def exam_result_detail(exam_id: int, result_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """One checked sheet in full: every question as that student saw it, their answer, the correct one."""
    exam, r, sheet = teacher_result(db, exam_id, result_id, user)
    return teacher_detail(db, exam, r, sheet)


@app.get("/api/exams/{exam_id}/results/{result_id}/sheet.jpg")
def exam_result_sheet(exam_id: int, result_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    _, r, _ = teacher_result(db, exam_id, result_id, user)
    if not r.image_path or not os.path.exists(r.image_path):
        raise HTTPException(404, "Sheet image missing")
    return FileResponse(r.image_path, media_type="image/jpeg")


@app.get("/api/exams/{exam_id}/results/{result_id}/{kind}.pdf")
def exam_result_pdf(exam_id: int, result_id: int, kind: str, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """kind = sheet (marked) or clean (unmarked scan)."""
    if kind not in ("sheet", "clean"):
        raise HTTPException(404, "Not found")
    _, r, _ = teacher_result(db, exam_id, result_id, user)
    path = r.image_path if kind == "sheet" else results.clean_path(r.image_path)
    if not r.image_path or not os.path.exists(path):
        raise HTTPException(404, "Image missing")
    name = "marked_sheet" if kind == "sheet" else "clean_scan"
    return Response(results.jpg_to_pdf(path), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}_{result_id}.pdf"'})


@app.get("/api/exams/{exam_id}/scan_batch/{job_id}/results.csv")
def scan_batch_csv(exam_id: int, job_id: str, user: User = Depends(auth.teacher_only)):
    job = JOBS.get(job_id)
    if not job or job["exam_id"] != exam_id or job["teacher_id"] != user.id:
        raise HTTPException(404, "Job not found")
    return Response(batch_csv(job["items"]), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="exam_{exam_id}_upload_{job_id}.csv"'})


@app.get("/api/exams/{exam_id}/results.csv")
def exam_csv(exam_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """All students of the exam with their details, marks and answers (also kept on disk as results.csv)."""
    exam = own_exam(db, exam_id, user)
    return Response(exam_results_csv(db, exam), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="exam_{exam_id}_results.csv"'})


@app.post("/api/exams/import")
async def import_exams(file: UploadFile = File(...), user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """Add exams exported from another copy of the app (export_exams.py). They become this teacher's; nothing here is
    overwritten, and exams that were imported before are skipped."""
    data = await file.read()
    if not 0 < len(data) <= MAX_UPLOAD:
        raise HTTPException(400, "Empty or too large file")
    try:
        n = transfer.merge_data(db.connection(), json.loads(data), user.id)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"Could not import this file: {e}")
    return n


@app.delete("/api/exams/{exam_id}")
def delete_exam(exam_id: int, confirm: str = "", user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """Permanently delete an exam: its questions, sheets, every checked result (so students lose access to them too),
    the QR links and the generated files. The caller must send ?confirm=delete."""
    exam = own_exam(db, exam_id, user)
    if confirm.strip().lower() != "delete":
        raise HTTPException(400, "Type 'delete' to confirm")
    if exam.status == "generating":
        raise HTTPException(409, "The sheets are still being generated - wait until that finishes, then delete")
    if any(j["exam_id"] == exam_id and j["status"] == "running" for j in JOBS.values()):
        raise HTTPException(409, "Sheets are being checked right now - wait until that finishes, then delete")
    removed = {"results": db.query(Result).filter_by(exam_id=exam_id).delete(), "sheets": len(exam.sheets), "questions": len(exam.questions)}
    db.delete(exam)                                   # cascades to questions and sheets
    db.commit()
    for job_id in [k for k, j in JOBS.items() if j["exam_id"] == exam_id]:
        JOBS.pop(job_id, None)
    shutil.rmtree(sheets.exam_dir(exam_id), ignore_errors=True)
    return {"deleted": True, **removed}


@app.put("/api/exams/{exam_id}/release")
def set_release(exam_id: int, body: ReleaseIn, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    exam = own_exam(db, exam_id, user)
    exam.marks_released = body.released
    db.commit()
    return exam_json(exam)


@app.get("/api/exams/{exam_id}/results")
def exam_results(exam_id: int, user: User = Depends(auth.teacher_only), db: Session = Depends(auth.get_db)):
    """Every graded sheet of this exam, for the teacher."""
    exam = own_exam(db, exam_id, user)
    by_sheet = {s.id: s for s in exam.sheets}
    rows = []
    for r in db.query(Result).filter_by(exam_id=exam.id).order_by(Result.created.desc()).all():
        student = db.query(User).filter_by(reg_no=r.registration, role="student").first()
        st = [results.status_of(r.answers.get(str(i + 1), "BLANK"), by_sheet[r.sheet_id].key[i]) for i in range(len(by_sheet[r.sheet_id].key))]
        rows.append({"id": r.id, "paper_id": by_sheet[r.sheet_id].paper_id, "registration": r.registration,
                     "counts": {k: st.count(k) for k in ("correct", "wrong", "blank", "invalid")},
                     "student": student.name if student else None, "score": r.score, "total": r.total,
                     "needs_review": bool(r.needs_review and not r.resolved), "review": results.review_state(r), "flags": r.flags,
                     "source": r.source, "scanned_at": r.created.isoformat()})
    return {"scanned": len(rows), "sheets": len(exam.sheets), "rows": rows,
            "needs_review": sum(1 for x in rows if x["needs_review"])}


def save_result(db: Session, exam: Exam, sheet: Sheet, res: dict, warped, fills, clean_page, source: str) -> Result:
    """Store a graded scan (marked sheet image, clean scan, answers). A later scan of the same sheet replaces it."""
    d = sheets.scan_dir(exam)
    layout = json.load(open(os.path.join(d, "layout.json")))
    image = os.path.join(d, "scans", f"{sheet.paper_id}.jpg")
    results.render_student_sheet(warped, layout, fills, res["answers"], sheet.key, image)
    results.save_render_inputs(warped, fills, image)
    results.write_jpeg(clean_page, results.clean_path(image))
    row = db.query(Result).filter_by(sheet_id=sheet.id).first() or Result(exam_id=exam.id, sheet_id=sheet.id)
    row.registration, row.answers, row.score, row.total = res["registration"], res["answers"], res["score"], res["total"]
    row.needs_review, row.flags, row.image_path, row.source = res["needs_review"], res["flags"], image, source
    row.resolved, row.original_answers, row.edited_at = False, None, None          # a fresh scan starts a fresh review
    row.created = dt.datetime.now(dt.timezone.utc)
    db.add(row)
    db.commit()
    return row


class SheetOfAnotherStudent(Exception):
    pass


# ── student portal ─────────────────────────────────────────────────────
class RegNoIn(BaseModel):
    reg_no: str


@app.post("/api/me/reg_no")
def set_reg_no(body: RegNoIn, user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    reg_no = clean_reg_no(body.reg_no)
    other = db.query(User).filter_by(reg_no=reg_no).first()
    if other and other.id != user.id:
        raise HTTPException(409, "That registration number is already taken")
    user.reg_no = reg_no
    db.add(user)
    db.commit()
    return user_json(user)


def result_summary(r: Result, exam: Exam, sheet: Sheet) -> dict:
    h = exam.header
    when = h.get("exam_date") or exam.created.date().isoformat()
    return {"id": r.id, "institution": h["institution_name"], "subject": h["course_name"], "exam": h["exam_title"],
            "exam_date": when, "paper_id": sheet.paper_id, "registration": r.registration, "score": r.score, "total": r.total,
            "max_marks": h.get("total_marks"), "needs_review": bool(r.needs_review and not r.resolved), "flags": r.flags,
            "review": results.review_state(r), "resolved": bool(r.resolved), "corrected": bool(r.original_answers),
            "scanned_at": r.created.isoformat(), "source": r.source}


def my_result(db: Session, user: User, result_id: int) -> Result:
    r = db.get(Result, result_id)
    if r is None or not user.reg_no or r.registration != user.reg_no or not db.get(Exam, r.exam_id).marks_released:
        raise HTTPException(404, "Result not found")             # marks stay hidden until the teacher releases them
    return r


def uploads_dir() -> str:
    """Where queued student uploads wait (next to the exams folder, e.g. /storage/uploads)."""
    return os.environ.get("OMR_UPLOADS_DIR") or os.path.normpath(os.path.join(os.path.dirname(sheets.exam_dir(0)), os.pardir, "uploads"))


MAX_QUEUED_PER_STUDENT = 3
SCAN_WAKE = threading.Event()                   # set when an upload arrives, so an idle worker starts at once
SCAN_CLAIM = threading.Lock()
SCAN_THREADS: list = []


@app.post("/api/me/scan")
async def student_scan(file: UploadFile = File(...), user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    """A student uploads their own filled answer sheet (scan or phone photo). The file is saved and queued straight away;
    a background worker grades it when its turn comes. Poll GET /api/me/scan/{job} for the result."""
    if not user.reg_no:
        raise HTTPException(409, "Add your registration number first")
    if db.query(ScanJob).filter(ScanJob.user_id == user.id, ScanJob.status.in_(("queued", "working"))).count() >= MAX_QUEUED_PER_STUDENT:
        raise HTTPException(429, "You already have sheets waiting to be checked - wait for them to finish")
    data = await file.read()
    if not 0 < len(data) <= MAX_UPLOAD:
        raise HTTPException(400, "Empty or too large file")
    name = os.path.basename(file.filename or "scan.jpg")
    os.makedirs(uploads_dir(), exist_ok=True)
    path = os.path.join(uploads_dir(), f"{user.id}_{random.getrandbits(64):016x}" + (os.path.splitext(name)[1].lower() or ".jpg"))
    with open(path, "wb") as f:
        f.write(data)
    job = ScanJob(user_id=user.id, path=path, name=name[:200])
    db.add(job)
    db.commit()
    start_scan_workers()
    SCAN_WAKE.set()
    return scan_job_json(db, job)


@app.get("/api/me/scan/{job_id}")
def student_scan_status(job_id: int, user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    job = db.get(ScanJob, job_id)
    if not job or job.user_id != user.id:
        raise HTTPException(404, "Upload not found")
    return scan_job_json(db, job)


def scan_job_json(db: Session, job: ScanJob) -> dict:
    out = {"job": job.id, "status": job.status}
    if job.status == "queued":                  # 1 = next in line
        out["position"] = db.query(ScanJob).filter(ScanJob.status == "queued", ScanJob.id < job.id).count() + 1
    elif job.status == "done":
        out["result"] = job.result
    elif job.status == "failed":
        out.update(error=job.error, code=job.error_code)
    return out


def start_scan_workers() -> None:
    """Start the background graders once (one per worker slot). Uploads left 'working' by a restart are queued again."""
    with SCAN_CLAIM:
        if any(t.is_alive() for t in SCAN_THREADS):
            return
        from .db import SessionLocal
        db = SessionLocal()
        try:
            db.query(ScanJob).filter_by(status="working").update({"status": "queued"})
            db.commit()
        finally:
            db.close()
        SCAN_THREADS[:] = [threading.Thread(target=scan_worker, daemon=True) for _ in range(workers.WORKERS)]
        for t in SCAN_THREADS:
            t.start()


def claim_next_scan() -> Optional[int]:
    from .db import SessionLocal
    with SCAN_CLAIM:
        db = SessionLocal()
        try:
            job = db.query(ScanJob).filter_by(status="queued").order_by(ScanJob.id).first()
            if job is None:
                return None
            job.status = "working"
            db.commit()
            return job.id
        finally:
            db.close()


def scan_worker() -> None:
    while True:
        SCAN_WAKE.clear()
        job_id = claim_next_scan()
        if job_id is None:
            SCAN_WAKE.wait(5)
            continue
        try:
            run_scan_job(job_id)
        except Exception:
            pass                                # never let one bad upload stop the worker


def run_scan_job(job_id: int) -> None:
    from .db import SessionLocal
    db = SessionLocal()
    try:
        job = db.get(ScanJob, job_id)
        try:
            with workers.slot():
                result = grade_student_upload(db, db.get(User, job.user_id), job.path, job.name)
            job.status, job.result = "done", result
        except HTTPException as e:
            db.rollback()
            job.status, job.error, job.error_code = "failed", str(e.detail), e.status_code
        except Exception as e:
            db.rollback()
            job.status, job.error, job.error_code = "failed", f"Could not check this sheet: {e}", 500
        db.commit()
        if os.path.exists(job.path):
            os.remove(job.path)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        db.query(ScanJob).filter(ScanJob.status.in_(("done", "failed")), ScanJob.created < old).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def grade_student_upload(db: Session, user: User, path: str, name: str) -> dict:
    """Grade a student's own sheet. The sheet is recognised by its pre-printed paper ID, and its registration number must
    be the student's own. Results saved this way are 'self-check'; an official result from the teacher is never overwritten."""
    def lookup(paper_id: str, count: int):
        sheet = db.query(Sheet).filter_by(paper_id=paper_id).first()
        exam = db.get(Exam, sheet.exam_id) if sheet else None
        return sheet if exam and exam.status == "ready" and exam.header.get("sheet_questions", len(exam.questions)) == count else None

    try:
        sheet, _ = sheets.identify_sheet(path, ALLOWED_COUNTS, lookup)
        exam = db.get(Exam, sheet.exam_id)
        if not exam.marks_released:
            raise PermissionError("Your teacher has not released the marks for this exam yet - try again later")
        existing = db.query(Result).filter_by(sheet_id=sheet.id).first()
        if existing:                                   # already checked (by the teacher or by an earlier self-check): do not grade again
            if existing.registration != user.reg_no:
                raise SheetOfAnotherStudent("This sheet has already been processed for a different student ID")
            official = existing.source == "teacher"
            return {"result_id": existing.id, "official": official, "already": True,
                    "message": "This sheet has already been processed" + (" by your teacher" if official else "") + " - here is the result."}
        d = sheets.scan_dir(exam)
        res, warped, fills = process_scan_full(path, os.path.join(d, "keys.json"), os.path.join(d, "layout.json"),
                                               os.path.join(d, "baseline.json"))
        clean_page = docscan.clean_page(warped)                 # the page the grader already straightened
    except PermissionError as e:
        raise HTTPException(409, str(e))
    except SheetOfAnotherStudent as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(422, str(e))
    if res["registration"] == "INVALID":
        raise HTTPException(422, "Could not read the registration number - fill all 5 digits clearly and scan again")
    if res["registration"] != user.reg_no:
        raise HTTPException(403, f"This sheet has registration number {res['registration']}, but your ID is {user.reg_no}")
    row = save_result(db, exam, sheet, res, warped, fills, clean_page, source="student")
    return {"result_id": row.id, "official": False, "message": "Marks calculated from your upload (self-check)."}


start_scan_workers()                            # also picks up uploads still queued from before a restart


@app.get("/api/me/results")
def my_results(user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    if not user.reg_no:
        return []
    rows = db.query(Result).filter_by(registration=user.reg_no).order_by(Result.created.desc()).all()
    return [result_summary(r, db.get(Exam, r.exam_id), db.get(Sheet, r.sheet_id)) for r in rows if db.get(Exam, r.exam_id).marks_released]


@app.get("/api/me/results/{result_id}")
def my_result_detail(result_id: int, user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    r = my_result(db, user, result_id)
    exam, sheet = db.get(Exam, r.exam_id), db.get(Sheet, r.sheet_id)
    items = results.review_items(exam, sheet, r.answers)
    counts = {k: sum(1 for i in items if i["status"] == k) for k in ("correct", "wrong", "blank", "invalid")}
    return dict(result_summary(r, exam, sheet), questions=items, counts=counts)


@app.get("/api/me/results/{result_id}/sheet.jpg")
def my_sheet_image(result_id: int, user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    r = my_result(db, user, result_id)
    if not r.image_path or not os.path.exists(r.image_path):
        raise HTTPException(404, "Sheet image missing")
    return FileResponse(r.image_path, media_type="image/jpeg")


@app.get("/api/me/results/{result_id}/clean.pdf")
def my_clean_scan(result_id: int, user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    r = my_result(db, user, result_id)
    clean = results.clean_path(r.image_path)
    if not r.image_path or not os.path.exists(clean):
        raise HTTPException(404, "Clean scan missing")
    return Response(results.jpg_to_pdf(clean), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="clean_scan_{result_id}.pdf"'})


@app.get("/api/me/results/{result_id}/sheet.pdf")
def my_sheet_pdf(result_id: int, user: User = Depends(auth.student_only), db: Session = Depends(auth.get_db)):
    r = my_result(db, user, result_id)
    if not r.image_path or not os.path.exists(r.image_path):
        raise HTTPException(404, "Sheet image missing")
    return Response(results.jpg_to_pdf(r.image_path), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="answer_sheet_{result_id}.pdf"'})


# ── document scanner (any logged-in user) ──────────────────────────────
@app.post("/api/scan/clean")
async def clean_document(file: UploadFile = File(...), mode: str = "gray", fmt: str = "jpg", user: User = Depends(auth.current_user)):
    """Photo of a page -> straight, shadow-free, white-paper document (CamScanner style). mode: gray | bw; fmt: jpg | pdf."""
    if mode not in ("gray", "bw") or fmt not in ("jpg", "pdf"):
        raise HTTPException(422, "mode must be gray or bw, fmt must be jpg or pdf")
    data = await file.read()
    if not 0 < len(data) <= MAX_UPLOAD:
        raise HTTPException(400, "Empty or too large file")
    def work():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "in" + (os.path.splitext(os.path.basename(file.filename or ""))[1].lower() or ".jpg"))
            with open(path, "wb") as f:
                f.write(data)
            try:
                page = docscan.scan_document(preprocess.load_image_color(path), mode)
            except Exception as e:
                raise HTTPException(422, f"Could not read that image: {e}")
            jpg = os.path.join(tmp, "out.jpg")
            results.write_jpeg(page, jpg)
            if fmt == "pdf":
                return Response(results.jpg_to_pdf(jpg), media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="scan.pdf"'})
            with open(jpg, "rb") as f:
                return Response(f.read(), media_type="image/jpeg")
    return await workers.run(work)


# ── QR code on a sheet: log in, then open the result in your own dashboard ──
QR_HITS: dict = {}                      # user id -> recent request times (simple guard against guessing tokens)


def qr_rate_limit(user_id: int, limit: int = 30, window: float = 60.0) -> None:
    import time
    now_ = time.time()
    hits = [t for t in QR_HITS.get(user_id, []) if now_ - t < window]
    if len(hits) >= limit:
        raise HTTPException(429, "Too many QR lookups - wait a minute and try again")
    QR_HITS[user_id] = hits + [now_]


@app.get("/api/qr/{token}")
def resolve_qr(token: str, user: User = Depends(auth.current_user), db: Session = Depends(auth.get_db)):
    """What to open after scanning a sheet's QR code. Nothing is revealed to anyone who is not logged in, and a logged-in
    user only learns about sheets that belong to them (a teacher: their own exam; a student: their own registration)."""
    qr_rate_limit(user.id)
    sheet = db.query(Sheet).filter_by(qr_token=token).first()
    if sheet is None:
        raise HTTPException(404, "This QR code is not recognised")
    exam = db.get(Exam, sheet.exam_id)
    result = db.query(Result).filter_by(sheet_id=sheet.id).first()
    if user.role == "teacher":
        if exam.teacher_id != user.id:
            raise HTTPException(403, "This sheet belongs to another teacher's exam")
        return {"role": "teacher", "status": "ok" if result else "not_checked", "exam_id": exam.id,
                "result_id": result.id if result else None}
    if result is None:
        return {"role": "student", "status": "not_checked"}
    if not user.reg_no or result.registration != user.reg_no:
        raise HTTPException(403, "This sheet belongs to another student")
    if not exam.marks_released:
        return {"role": "student", "status": "not_released"}
    return {"role": "student", "status": "ok", "result_id": result.id}


@app.get("/r/{token}")
def public_page(token: str):
    return FileResponse(os.path.join(config.ROOT_DIR, "web", "index.html"))


@app.api_route("/api/health", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])      # uptime monitors (e.g. UptimeRobot) send HEAD by default
def health():
    return {"ok": True}


# ── static page ────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(config.ROOT_DIR, "web", "index.html"))

"""Build per-student answer sheets: shuffle, render the Jinja template, compile with pdflatex."""
import csv
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import sys

from omr import config
from . import textconv, workers
from omr.calibration import compute_baseline

sys.path.insert(0, os.path.join(config.ROOT_DIR, "sheet"))
import build_sheet as bs  # noqa: E402

LETTERS = "ABCD"
LATEX_TIMEOUT_S = 90
# "paranoid" file access: LaTeX may not read absolute paths or parent folders (question text is raw LaTeX)
LATEX_ENV = dict(os.environ, openin_any="p", openout_any="p")


def public_url() -> str:
    """Address the QR codes point to (must be reachable from the students' phones): env OMR_PUBLIC_URL."""
    url = os.environ.get("OMR_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    return url if re.fullmatch(r"https?://[A-Za-z0-9.\-:]+(/[A-Za-z0-9.\-/]*)?", url) else "http://localhost:8000"


def qr_link(token: str) -> str:
    return f"{public_url()}/r/{token}"


class CompileError(RuntimeError):
    pass


def exam_dir(exam_id: int) -> str:
    return os.path.join(os.environ.get("OMR_EXAMS_DIR", os.path.join(config.OUTPUT_DIR, "exams")), str(exam_id))


def scan_dir(exam) -> str:
    """The exam's folder, with the files scanning needs (layout, answer keys, ID-bubble baseline). When they are missing -
    a fresh server disk, or an exam copied in from another database - they are rebuilt from the database."""
    d = exam_dir(exam.id)
    layout, keys, baseline = (os.path.join(d, n) for n in ("layout.json", "keys.json", "baseline.json"))
    if os.path.exists(layout) and os.path.exists(keys) and os.path.exists(baseline):
        return d
    os.makedirs(d, exist_ok=True)
    k = exam.header.get("sheet_questions", len(exam.questions))
    fmt_layout, fmt_baseline = format_assets(k)         # same layout as every exam with k questions (compiled once, cached)
    for src, dst in ((fmt_layout, layout), (fmt_baseline, baseline)):
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
    if not os.path.exists(keys):
        with open(keys, "w") as f:
            json.dump({s.paper_id: s.key for s in exam.sheets}, f)
    return d


def compile_tex(tex: str, out_pdf: str) -> None:
    """Run pdflatex twice (TikZ overlay needs two passes) in a scratch folder; raise CompileError with the log."""
    with workers.slot(), tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "sheet.tex"), "w") as f:
            f.write(tex)
        for _ in range(2):
            try:
                subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "sheet.tex"],
                               cwd=tmp, env=LATEX_ENV, capture_output=True, timeout=LATEX_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise CompileError("LaTeX took too long (possible infinite loop in your question text).")
            if not os.path.exists(os.path.join(tmp, "sheet.pdf")):
                log = open(os.path.join(tmp, "sheet.log"), errors="replace").read() if os.path.exists(os.path.join(tmp, "sheet.log")) else ""
                bang = [l for l in log.splitlines() if l.startswith("!")]
                raise CompileError("LaTeX error: " + (" / ".join(bang[:3]) or log[-400:]))
        os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
        shutil.move(os.path.join(tmp, "sheet.pdf"), out_pdf)


def header_dict(header: dict, n_reg_digits: int) -> dict:
    return dict(header, roll_no_digits=n_reg_digits, roll_no_prefix=header.get("roll_no_prefix", ""))


def shuffled_paper(pool: List[dict], k: int, rng: random.Random, shuffle_q: bool, shuffle_o: bool):
    """Pick k questions for one student from the pool (a random subset if the pool is larger).
    Returns (displayed questions, key letters, indices used, option orders)."""
    order = sorted(rng.sample(range(len(pool)), k))
    if shuffle_q:
        rng.shuffle(order)
    shown, key, opt_orders = [], [], []
    for qi in order:
        q = pool[qi]
        oo = list(range(4))
        if shuffle_o:
            rng.shuffle(oo)
        plain = [q["options"][i] for i in oo]
        shown.append({"text": textconv.to_latex(q["text"]), "options": [{"text": textconv.to_latex(o)} for o in plain]})
        key.append(LETTERS[oo.index(q["correct"])])
        opt_orders.append(oo)
    return shown, key, order, opt_orders


class ExamSheetBuilder:
    """Holds the layout for one exam format (depends on the question count) and renders individual sheets."""

    def __init__(self, n_questions: int):
        self.cfg = bs.load_config(n_questions)
        self.model = bs.build_model(self.cfg)
        self.layout = bs.build_layout(self.cfg, self.model)
        self.n_reg = self.cfg["id_sections"]["registration"]["digits"]

    def tex(self, header: dict, shown: List[dict], paper_id: Optional[str], qr_text: Optional[str], qr_label: str = "",
            page1: Optional[int] = None) -> str:
        exam = dict(header_dict(header, self.n_reg), questions=shown)
        if page1 is not None:
            exam["page1_questions"] = page1
        model = dict(self.model, **bs.text_context(self.cfg, exam), paper_id_digits=paper_id, qr_text=qr_text, qr_label=qr_label)
        return bs.render_tex(self.cfg, model)


PAGE1_BUDGET_MM = 186.0          # height under the header, beside the answer strip
LINE_MM = 4.4


def estimate_mm(q: dict) -> float:
    """Rough height of one question at 11 pt in the narrow (116 mm) column; options are one per line."""
    lines = max(1, -(-len(q["text"]) // 60))
    opts = sum(max(1, -(-(len(o["text"]) + 3) // 52)) for o in q["options"]) * (LINE_MM + 0.7) + 3.5
    return lines * LINE_MM + opts + 3.5


def first_page_count(shown: List[dict]) -> int:
    """How many questions to try beside the answer strip; the rest go on a full-width page 2."""
    used, k = 0.0, 0
    for q in shown:
        used += estimate_mm(q)
        if used > PAGE1_BUDGET_MM:
            break
        k += 1
    return max(k, 1)


def compile_paper(builder: "ExamSheetBuilder", header: dict, shown: List[dict], paper_id, qr, label, out_pdf: str) -> None:
    """Compile a sheet, adjusting the page-1 question count until page 1 holds exactly what we put there.

    The estimate is only a starting point: shrink while it overflows, and try one more question if it all fits."""
    import fitz
    n, k = len(shown), first_page_count(shown)

    def attempt(count: int) -> bool:
        compile_tex(builder.tex(header, shown, paper_id, qr, label, page1=count), out_pdf)
        with fitz.open(out_pdf) as d:
            p1 = d[0].get_text()
            p2 = d[1].get_text() if d.page_count > 1 else ""
        return f"Q{count}." in p1 and ((not p2.strip()) if count == n else (f"Q{count + 1}." in p2 and "Paper ID" in p2))

    if attempt(k):
        if k < n and attempt(k + 1):
            return
        if k < n:
            attempt(k)                      # the extra question did not fit: rebuild the good version
        return
    while k > 1:
        k -= 1
        if attempt(k):
            return


def preview_pdf(header: dict, questions: List[dict], k: int, out_pdf: str) -> None:
    """One sample sheet (paper ID 0000, first k questions) so the teacher can check the layout before generating."""
    b = ExamSheetBuilder(k)
    shown, _, _, _ = shuffled_paper(questions[:k], k, random.Random(0), False, False)
    compile_paper(b, header, shown, "0" * b.cfg["id_sections"]["paper_id"]["digits"], "PREVIEW", "PREVIEW", out_pdf)


def generate_exam(exam_id: int) -> None:
    """Background job: compile every student's sheet, then write layout/baseline/keys for scanning."""
    from .db import Exam, SessionLocal
    db = SessionLocal()
    try:
        exam = db.get(Exam, exam_id)
        questions = [dict(text=q.text, options=q.options, correct=q.correct) for q in exam.questions]
        k = exam.header.get("sheet_questions", len(questions))
        builder = ExamSheetBuilder(k)
        out = exam_dir(exam_id)
        os.makedirs(os.path.join(out, "sheets"), exist_ok=True)
        layout_path = os.path.join(out, "layout.json")
        with open(layout_path, "w") as f:
            json.dump(builder.layout, f)

        def work(sheet):
            rng = random.Random(secrets.randbits(64))
            shown, key, order, opt_orders = shuffled_paper(questions, k, rng, exam.shuffle_questions, exam.shuffle_options)
            qr = qr_link(sheet.qr_token)
            label = f"ID {sheet.paper_id}"
            pdf = os.path.join(out, "sheets", f"{sheet.paper_id}.pdf")
            compile_paper(builder, exam.header, shown, sheet.paper_id, qr, label, pdf)
            return sheet.id, key, order, opt_orders, pdf

        # blank sheet -> baseline (digits printed in the ID bubbles), same layout
        blank_pdf = os.path.join(out, "blank.pdf")
        shown, _, _, _ = shuffled_paper(questions[:k], k, random.Random(0), False, False)
        compile_paper(builder, exam.header, shown, None, None, "", blank_pdf)
        with open(os.path.join(out, "baseline.json"), "w") as f:
            json.dump(compute_baseline(blank_pdf, layout_path), f)

        keys: Dict[str, List[str]] = {}
        with ThreadPoolExecutor(max_workers=workers.WORKERS) as pool:
            for sid, key, order, opt_orders, pdf in pool.map(work, list(exam.sheets)):
                sheet = next(s for s in exam.sheets if s.id == sid)
                sheet.key, sheet.question_order, sheet.option_orders, sheet.pdf_path = key, order, opt_orders, pdf
                keys[sheet.paper_id] = key
                exam.progress += 1
                db.commit()
        with open(os.path.join(out, "keys.json"), "w") as f:
            json.dump(keys, f)
        merge_pdfs([s.pdf_path for s in exam.sheets], os.path.join(out, "all_sheets.pdf"))
        exam.status = "ready"
        db.commit()
    except Exception as e:  # report to the teacher instead of dying silently in the thread
        db.rollback()
        exam = db.get(Exam, exam_id)
        exam.status, exam.error = "failed", str(e)[:1000]
        db.commit()
    finally:
        db.close()


def merge_pdfs(paths: List[str], out_pdf: str) -> None:
    """One PDF with every student's sheet in order (short papers are exactly one page each).

    A multi-page paper with an odd page count gets a blank page after it, so in double-sided
    printing the next student's paper starts on a fresh sheet instead of the back of the last page.
    """
    import fitz
    merged = fitz.open()
    for p in paths:
        with fitz.open(p) as d:
            merged.insert_pdf(d)
            if d.page_count > 1 and d.page_count % 2:
                last = d[-1].rect
                merged.new_page(width=last.width, height=last.height)
    merged.save(out_pdf)


def manifest_csv(exam) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["student_no", "paper_id", "qr_text", "answer_key"])
    for s in exam.sheets:
        w.writerow([s.student_no, s.paper_id, qr_link(s.qr_token), " ".join(s.key)])
    return buf.getvalue()



def formats_dir() -> str:
    return os.environ.get("OMR_FORMATS_DIR", os.path.join(config.OUTPUT_DIR, "formats"))


def format_assets(count: int) -> tuple:
    """(layout.json, baseline.json) for the sheet format with `count` questions, built once and cached.
    Every exam with that question count shares the same layout, so a sheet can be recognised before we know its exam."""
    builder = ExamSheetBuilder(count)
    out = os.path.join(formats_dir(), str(count))
    layout_path, baseline_path = os.path.join(out, "layout.json"), os.path.join(out, "baseline.json")
    if os.path.exists(layout_path) and os.path.exists(baseline_path) and json.load(open(layout_path)) == builder.layout:
        return layout_path, baseline_path
    os.makedirs(out, exist_ok=True)
    with open(layout_path, "w") as f:
        json.dump(builder.layout, f)
    blank = [{"text": "x", "options": [{"text": o} for o in "abcd"]} for _ in range(count)]
    compile_paper(builder, {"institution_name": "x", "course_name": "x", "exam_title": "x", "total_marks": count},
                  blank, None, None, "", os.path.join(out, "blank.pdf"))
    with open(baseline_path, "w") as f:
        json.dump(compute_baseline(os.path.join(out, "blank.pdf"), layout_path), f)
    return layout_path, baseline_path


def identify_sheet(path: str, allowed_counts: List[int], lookup) -> "tuple":
    """Find which printed sheet a scan/photo is by reading its pre-printed paper ID with each format's template.
    `lookup(paper_id, count)` returns the matching Sheet row or None. Returns (sheet, count, located), where
    located = (oriented image, frame corners) can be passed on to grading so the page is not found twice."""
    from omr import bubbles, grader, markers, pipeline, preprocess, warp
    img = preprocess.load_image(path)
    first_layout, _ = format_assets(allowed_counts[0])
    oriented, corners = pipeline.locate_page(img, first_layout)            # the frame is identical in every format
    for count in allowed_counts:
        layout_path, baseline_path = format_assets(count)
        warped = warp.warp_image(oriented, corners, layout_path)
        fills = bubbles.process_sheet_bubbles(warped, layout_path, baseline_path)
        pid = "".join(grader.decode_id_column(col) for col in fills["paper_id"])
        if "?" not in pid:
            sheet = lookup(pid, count)
            if sheet is not None:
                return sheet, count, (oriented, corners)
    raise ValueError("Could not read the paper ID on this sheet. Make sure the whole sheet is in view, well lit and flat.")

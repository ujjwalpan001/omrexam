"""End-to-end: sign up, generate sheets, check unique IDs/keys, scan a filled sheet."""
import json
import os
import tempfile
import time


import cv2
import numpy as np
import fitz
import pytest

_tmp = tempfile.mkdtemp()
os.environ["OMR_DB"] = os.path.join(_tmp, "test.db")          # must be set before importing the app
os.environ["OMR_EXAMS_DIR"] = os.path.join(_tmp, "exams")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from omr import config, preprocess  # noqa: E402

def block(i):
    return f"Q {i} of 100% & $x^{i}$?\nA) a\nB) b_1\nC) c\nD) d\nAnswer: {'ABCD'[i % 4]}"


def exam_payload(n, students=2, pool=None):
    return {
        "institution_name": "Test Uni", "course_name": "C1", "exam_title": "Quiz", "test_number": "1",
        "num_students": students, "shuffle_questions": True, "shuffle_options": True, "num_questions": n,
        "questions_text": "\n\n".join(block(i) for i in range(pool or n)),
    }


def teacher(client):
    r = client.post("/api/register", json={"name": "T", "email": "t@t.com", "password": "password123", "role": "teacher"})
    assert r.status_code == 200
    return client


def wait_ready(client, exam_id):
    for _ in range(240):
        e = client.get(f"/api/exams/{exam_id}").json()
        if e["status"] != "generating":
            return e
        time.sleep(0.5)
    raise AssertionError("generation timed out")


class Finished:
    """What a queued student upload ended as, shaped like a response (status_code, json(), text)."""
    def __init__(self, code, body):
        self.status_code, self.body, self.text = code, body, json.dumps(body)

    def json(self):
        return self.body


def student_upload(client, path, name, mime):
    """POST a sheet to the student queue and wait for the background worker to grade it."""
    r = client.post("/api/me/scan", files={"file": (name, open(path, "rb"), mime)})
    if r.status_code != 200:
        return r
    j = r.json()
    for _ in range(240):
        if j["status"] in ("done", "failed"):
            return Finished(200, j["result"]) if j["status"] == "done" else Finished(j["code"], {"detail": j["error"]})
        time.sleep(0.25)
        j = client.get(f"/api/me/scan/{j['job']}").json()
    raise AssertionError("queued upload never finished")


def login_teacher():
    c = TestClient(app)
    if c.post("/api/login", json={"email": "t@t.com", "password": "password123"}).status_code != 200:
        teacher(c)
    return c


def test_health_answers_get_and_head_without_login():
    c = TestClient(app)
    for path in ("/api/health", "/health"):
        assert c.get(path).json() == {"ok": True}
        assert c.head(path).status_code == 200                                                # UptimeRobot sends HEAD


def test_auth_rules():
    c = TestClient(app)
    assert c.get("/api/exams").status_code == 401
    c.post("/api/register", json={"name": "S", "email": "s@t.com", "password": "password123", "role": "student", "reg_no": "11111"})
    assert c.get("/api/exams").status_code == 403
    assert TestClient(app).post("/api/login", json={"email": "s@t.com", "password": "wrongwrong"}).status_code == 401


def test_bad_question_text_rejected():
    c = login_teacher()
    bad = exam_payload(10)
    bad["questions_text"] = "No options in this one\n\n" + bad["questions_text"]
    assert c.post("/api/exams/preview", json=bad).status_code == 422
    bad["questions_text"] = "Try $\\input{/etc/passwd}$\nA) a\nB) b\nC) c\nD) d\nAnswer: A\n\n" + block(1) * 1
    assert c.post("/api/exams/preview", json=bad).status_code == 422
    bad["questions_text"] = "\n\n".join(block(i).rsplit("\nAnswer", 1)[0] for i in range(10))     # no answers
    assert "no correct answer" in c.post("/api/exams/preview", json=bad).text
    assert c.post("/api/exams/preview", json=exam_payload(15, pool=12)).status_code == 422       # pool smaller than sheet
    assert c.post("/api/exams/preview", json=exam_payload(10)).status_code == 200      # specials are escaped, compiles


def test_only_allowed_question_counts():
    c = login_teacher()
    for n in (4, 12, 20, 25, 30):
        assert c.post("/api/exams", json=exam_payload(n)).status_code == 422


def filled_scan(exam_dir, paper_id, work, reg=(1, 2, 3, 4, 5)):
    """Render the sheet, tick the correct answers and registration 12345; return a PNG path."""
    key = json.load(open(os.path.join(exam_dir, "keys.json")))[paper_id]
    layout = json.load(open(os.path.join(exam_dir, "layout.json")))
    img = preprocess.load_image(os.path.join(exam_dir, "sheets", f"{paper_id}.pdf"))
    px = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    r = int(layout["bubble_radius_mm"] * px * 0.8)

    def dot(b):
        cv2.circle(img, (int(b["cx"] * px), int(b["cy"] * px)), r, 40, -1)

    for q in layout["questions"]:
        dot(q["options"][key[q["q"] - 1]])
    for col, digit in zip(layout["registration"]["columns"], reg):
        dot(col[digit])
    path = os.path.join(work, f"{paper_id}.png")
    cv2.imwrite(path, img)
    return path


def make_exam(c, n, pool=None, release=True, students=2):
    r = c.post("/api/exams", json=exam_payload(n, students=students, pool=pool))
    assert r.status_code == 200, r.text
    e = wait_ready(c, r.json()["id"])
    assert e["status"] == "ready", e["error"]
    assert e["marks_released"] is False                                   # hidden until the teacher releases them
    if release:
        assert c.put(f"/api/exams/{e['id']}/release", json={"released": True}).json()["marks_released"] is True
    ids = [s["paper_id"] for s in e["sheets"]]
    assert len(set(ids)) == len(ids) and len({s["qr"] for s in e["sheets"]}) == len(ids)
    return e, ids, os.path.join(os.environ["OMR_EXAMS_DIR"], str(e["id"]))


@pytest.mark.parametrize("n", [10, 15])
def test_generate_and_scan(n):
    c = login_teacher()
    e, ids, d = make_exam(c, n)
    assert e["num_questions"] == n
    for i in ids:
        assert len(i) == 6
        pages = fitz.open(os.path.join(d, "sheets", f"{i}.pdf"))
        assert all(p.get_text().strip() for p in pages)                                         # no blank pages
    merged = fitz.open(os.path.join(d, "all_sheets.pdf")).page_count
    counts = [fitz.open(os.path.join(d, "sheets", f"{i}.pdf")).page_count for i in ids]
    assert merged == sum(k + (k > 1 and k % 2) for k in counts)                                # odd multi-page papers padded
    assert c.get(f"/api/exams/{e['id']}/print.pdf").status_code == 200
    z = c.get(f"/api/exams/{e['id']}/download.zip")
    assert z.status_code == 200 and z.content[:2] == b"PK"
    pdf = c.get(f"/api/exams/{e['id']}/sheets/{ids[0]}.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    scan = filled_scan(d, ids[0], _tmp)
    res = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()
    assert res["paper_id"] == ids[0] and res["registration"] == "12345"
    assert res["score"] == n and res["needs_review"] is False


def test_question_bank_gives_each_student_a_random_subset():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, pool=60)
    assert e["num_questions"] == 10 and e["pool_size"] == 60
    keys = json.load(open(os.path.join(d, "keys.json")))
    assert all(len(k) == 10 for k in keys.values())
    scan = filled_scan(d, ids[0], _tmp)
    res = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()
    assert res["paper_id"] == ids[0] and res["score"] == 10


def test_student_sees_own_results_only():
    c = login_teacher()
    e, ids, d = make_exam(c, 10)
    scan = filled_scan(d, ids[0], _tmp)
    res = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()
    assert res["saved"] is True and res["registration"] == "12345"

    def student(email, reg):
        s = TestClient(app)
        return s, s.post("/api/register", json={"name": email, "email": email, "password": "password123", "role": "student", "reg_no": reg})

    assert student("bad@t.com", "123")[1].status_code == 422                      # wrong length
    s, r = student("me@t.com", "12345")
    assert r.status_code == 200 and r.json()["reg_no"] == "12345"
    assert student("dup@t.com", "12345")[1].status_code == 409                    # registration numbers are unique

    rows = [x for x in s.get("/api/me/results").json() if x["paper_id"] == ids[0]]
    assert len(rows) == 1 and rows[0]["subject"] == "C1" and rows[0]["exam"] == "Quiz" and rows[0]["paper_id"] == ids[0]
    assert rows[0]["score"] == 10 and rows[0]["total"] == 10
    detail = s.get(f"/api/me/results/{rows[0]['id']}").json()
    assert len(detail["questions"]) == 10 and detail["counts"]["correct"] == 10
    q1 = detail["questions"][0]
    assert q1["status"] == "correct" and q1["your"] == q1["correct"] and len(q1["options"]) == 4
    img = s.get(f"/api/me/results/{rows[0]['id']}/sheet.jpg")
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    assert s.get(f"/api/me/results/{rows[0]['id']}/sheet.pdf").content[:4] == b"%PDF"

    other, _ = student("other@t.com", "54321")                                       # a different student cannot open it
    assert other.get("/api/me/results").json() == []
    assert other.get(f"/api/me/results/{rows[0]['id']}").status_code == 404
    assert c.get("/api/me/results").status_code == 403                               # teachers have no student portal


def test_wrong_answers_are_marked_wrong():
    c = login_teacher()
    e, ids, d = make_exam(c, 10)
    layout = json.load(open(os.path.join(d, "layout.json")))
    key = json.load(open(os.path.join(d, "keys.json")))[ids[1]]
    img = preprocess.load_image(os.path.join(d, "sheets", f"{ids[1]}.pdf"))
    px = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    r = int(layout["bubble_radius_mm"] * px * 0.8)
    for col, digit in zip(layout["registration"]["columns"], [9, 8, 7, 6, 5]):
        b = col[digit]
        cv2.circle(img, (int(b["cx"] * px), int(b["cy"] * px)), r, 40, -1)
    for q in layout["questions"]:
        if q["q"] <= 3:                                     # Q1-3 wrong on purpose, Q4 left blank, rest correct
            letter = "ABCD"[("ABCD".index(key[q["q"] - 1]) + 1) % 4]
        elif q["q"] == 4:
            continue
        else:
            letter = key[q["q"] - 1]
        b = q["options"][letter]
        cv2.circle(img, (int(b["cx"] * px), int(b["cy"] * px)), r, 40, -1)
    path = os.path.join(_tmp, "wrong.png")
    cv2.imwrite(path, img)
    assert c.post(f"/api/exams/{e['id']}/scan", files={"file": ("w.png", open(path, "rb"), "image/png")}).json()["saved"]
    s = TestClient(app)
    s.post("/api/register", json={"name": "w", "email": "w@t.com", "password": "password123", "role": "student", "reg_no": "98765"})
    rid = s.get("/api/me/results").json()[0]["id"]           # only this student's registration number, 98765
    det = s.get(f"/api/me/results/{rid}").json()
    assert det["counts"] == {"correct": 6, "wrong": 3, "blank": 1, "invalid": 0}
    assert [q["status"] for q in det["questions"][:4]] == ["wrong", "wrong", "wrong", "blank"]


def test_student_logs_in_with_id_and_any_password():
    s = TestClient(app)
    r = s.post("/api/register", json={"name": "Id User", "password": "x", "role": "student", "reg_no": "24680"})
    assert r.status_code == 200
    again = TestClient(app)
    assert again.post("/api/login", json={"email": "24680", "password": "x"}).status_code == 200
    assert again.get("/api/me").json()["role"] == "student"
    assert TestClient(app).post("/api/login", json={"email": "24680", "password": "y"}).status_code == 401
    weak_teacher = TestClient(app).post("/api/register", json={"name": "T2", "email": "t2@t.com", "password": "abc", "role": "teacher"})
    assert weak_teacher.status_code == 422


def test_document_scanner_endpoint_and_clean_pdf_for_student():
    c = login_teacher()
    photo = os.path.join(_tmp, "photo.jpg")
    from test_docscan import filled_sheet, photo_of
    cv2.imwrite(photo, photo_of(filled_sheet()), [cv2.IMWRITE_JPEG_QUALITY, 85])
    r = c.post("/api/scan/clean?fmt=jpg&mode=gray", files={"file": ("p.jpg", open(photo, "rb"), "image/jpeg")})
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    pdf = c.post("/api/scan/clean?fmt=pdf&mode=bw", files={"file": ("p.jpg", open(photo, "rb"), "image/jpeg")})
    assert pdf.content[:4] == b"%PDF"
    assert c.post("/api/scan/clean?mode=sepia", files={"file": ("p.jpg", open(photo, "rb"), "image/jpeg")}).status_code == 422
    assert TestClient(app).post("/api/scan/clean", files={"file": ("p.jpg", open(photo, "rb"), "image/jpeg")}).status_code == 401

    e, ids, d = make_exam(c, 10)
    scan = filled_scan(d, ids[0], _tmp, reg=(3, 1, 4, 1, 5))
    assert c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()["saved"]
    s = TestClient(app)
    assert s.post("/api/register", json={"name": "cl", "password": "p", "role": "student", "reg_no": "31415"}).status_code == 200
    rid = s.get("/api/me/results").json()[0]["id"]
    assert s.get(f"/api/me/results/{rid}/clean.pdf").content[:4] == b"%PDF"


def new_student(reg, password="p"):
    s = TestClient(app)
    assert s.post("/api/register", json={"name": f"s{reg}", "password": password, "role": "student", "reg_no": reg}).status_code == 200
    return s


@pytest.mark.parametrize("n,reg", [(10, "27182"), (15, "16180")])
def test_student_scans_own_sheet(n, reg):
    c = login_teacher()
    e, ids, d = make_exam(c, n)
    s = new_student(reg)
    digits = tuple(int(x) for x in reg)
    scan = filled_scan(d, ids[0], _tmp, reg=digits)
    up = student_upload(s, scan, "mine.png", "image/png")
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["official"] is False
    detail = s.get(f"/api/me/results/{body['result_id']}").json()
    assert detail["source"] == "student" and detail["score"] == n and detail["paper_id"] == ids[0]

    # someone else's sheet is refused
    stranger = new_student(reg[::-1] if reg[::-1] != reg else "99999")
    refused = student_upload(stranger, scan, "x.png", "image/png")
    assert refused.status_code == 403 and "different student ID" in refused.text          # the sheet was already processed for someone else
    fresh = filled_scan(d, ids[1], _tmp, reg=digits)                                        # an unprocessed sheet: registration mismatch
    mismatch = student_upload(stranger, fresh, "x.png", "image/png")
    assert mismatch.status_code == 403 and "registration number" in mismatch.text

    # the teacher's scan is official and is not overwritten by a later self-check
    assert c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()["saved"]
    again = student_upload(s, scan, "mine.png", "image/png").json()
    assert again["official"] is True
    assert s.get(f"/api/me/results/{again['result_id']}").json()["source"] == "teacher"


def test_student_scans_a_phone_photo_and_bad_uploads():
    from test_docscan import photo_of
    c = login_teacher()
    e, ids, d = make_exam(c, 10)
    s = new_student("55555")
    scan = filled_scan(d, ids[1], _tmp, reg=(5, 5, 5, 5, 5))
    photo = os.path.join(_tmp, "student_photo.jpg")
    cv2.imwrite(photo, photo_of(cv2.imread(scan, 0)), [cv2.IMWRITE_JPEG_QUALITY, 88])
    up = student_upload(s, photo, "p.jpg", "image/jpeg")
    assert up.status_code == 200, up.text
    assert s.get(f"/api/me/results/{up.json()['result_id']}").json()["score"] == 10
    blank = os.path.join(_tmp, "blank.png")
    cv2.imwrite(blank, np.full((600, 400), 255, np.uint8))
    assert student_upload(s, blank, "b.png", "image/png").status_code == 422
    assert student_upload(c, blank, "b.png", "image/png").status_code == 403     # teachers cannot


def sheet_png(d, paper_id, reg, work, name):
    return filled_scan(d, paper_id, work, reg=reg)


def pdf_of_images(paths, out):
    import fitz
    doc = fitz.open()
    for p in paths:
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, filename=p)
    doc.save(out)
    return out


def wait_job(c, exam_id, job):
    for _ in range(240):
        j = c.get(f"/api/exams/{exam_id}/scan_batch/{job}").json()
        assert j["phase"] in ("queued", "preparing", "checking", "finished") and j["elapsed_seconds"] >= 0
        if j["phase"] == "checking" and j["current"]:                      # live progress for the teacher's screen
            assert j["current"]["step"] in ("wait", "find", "read", "grade", "save") and j["current"]["page"]
        if j["status"] == "finished":
            assert j["phase"] == "finished" and j["current"] == {} and j["eta_seconds"] is None and j["done"] == j["total"]
            return j
        time.sleep(0.5)
    raise AssertionError("batch timed out")


def test_batch_pdf_release_toggle_and_qr_card():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, release=False, students=4)
    assert all(s["qr"].startswith("http") and "/r/" in s["qr"] for s in e["sheets"])
    tokens = [s["qr"].rsplit("/", 1)[1] for s in e["sheets"]]
    regs = [(4, 4, 4, 4, 1), (4, 4, 4, 4, 2), (4, 4, 4, 4, 3)]
    pngs = [filled_scan(d, ids[i], _tmp, reg=regs[i]) for i in range(3)]
    pdf = pdf_of_images(pngs, os.path.join(_tmp, "batch.pdf"))          # one PDF, one student per page
    stranger = os.path.join(_tmp, "stranger.png")
    cv2.imwrite(stranger, np.full((1200, 900), 255, np.uint8))           # a page we cannot read
    files = [("files", ("all.pdf", open(pdf, "rb"), "application/pdf")), ("files", ("bad.png", open(stranger, "rb"), "image/png"))]
    started = c.post(f"/api/exams/{e['id']}/scan_batch", files=files).json()
    assert started["total"] == 4
    job = wait_job(c, e["id"], started["job"])
    ok = [i for i in job["items"] if i["ok"]]
    assert len(ok) == 3 and sorted(i["paper_id"] for i in ok) == sorted(ids[:3]) and all(i["score"] == 10 for i in ok)
    assert [i["ok"] for i in job["items"]].count(False) == 1
    res = c.get(f"/api/exams/{e['id']}/results").json()
    assert res["scanned"] == 3 and res["sheets"] == 4
    for row in res["rows"]:                                              # every page of the PDF has its own full detail view
        det = c.get(f"/api/exams/{e['id']}/results/{row['id']}").json()
        assert det["paper_id"] == row["paper_id"] and len(det["questions"]) == 10 and det["score"] == 10
        assert c.get(f"/api/exams/{e['id']}/results/{row['id']}/sheet.jpg").content[:2] == b"\xff\xd8"
        assert c.get(f"/api/exams/{e['id']}/results/{row['id']}/clean.pdf").content[:4] == b"%PDF"

    student = new_student("44441")                                      # registration 44441 belongs to the first sheet
    hidden = student.get("/api/me/results").json()                       # not released: listed as checked, but no marks
    assert len(hidden) == 1 and hidden[0]["released"] is False and "score" not in hidden[0] and "flags" not in hidden[0]
    assert student.get(f"/api/me/results/{hidden[0]['id']}").status_code == 404
    early = student_upload(student, pngs[0], "m.png", "image/png")        # before release: checked, but no marks shown
    assert early.status_code == 200 and early.json()["released"] is False and "score" not in early.json()
    public = TestClient(app)                                            # the QR link alone reveals nothing
    assert public.get(f"/api/qr/{tokens[0]}").status_code == 401
    assert public.get(f"/api/public/r/{tokens[0]}").status_code == 404   # the old public card no longer exists
    assert public.get(f"/r/{tokens[0]}").status_code == 200              # the page a phone opens (it asks for a login)
    assert all(len(tk) == 24 for tk in tokens)                           # 96-bit random tokens

    other_student, other_teacher = new_student("44449"), TestClient(app)
    other_teacher.post("/api/register", json={"name": "T8", "email": "t8@t.com", "password": "password123", "role": "teacher"})
    assert other_teacher.get(f"/api/qr/{tokens[0]}").status_code == 403  # not their exam
    assert other_student.get(f"/api/qr/{tokens[0]}").status_code == 403  # not their sheet
    assert student.get(f"/api/qr/nope").status_code == 404

    mine = c.get(f"/api/qr/{tokens[0]}").json()                          # teacher: opens the result in the teacher dashboard
    assert mine["role"] == "teacher" and mine["status"] == "ok" and mine["exam_id"] == e["id"] and mine["result_id"]
    unscanned = c.get(f"/api/qr/{tokens[3]}").json()
    assert unscanned["status"] == "not_checked" and unscanned["result_id"] is None
    assert student.get(f"/api/qr/{tokens[0]}").json() == {"role": "student", "status": "not_released"}    # no details before release
    assert student.get(f"/api/qr/{tokens[3]}").json()["status"] == "not_checked"

    assert c.put(f"/api/exams/{e['id']}/release", json={"released": True}).json()["marks_released"] is True
    opened = student.get(f"/api/qr/{tokens[0]}").json()                  # student: opens the result in the student dashboard
    assert opened["status"] == "ok" and opened["result_id"] == mine["result_id"]
    assert student.get(f"/api/me/results/{opened['result_id']}").json()["score"] == 10
    listed = student.get("/api/me/results").json()
    assert len(listed) == 1 and listed[0]["released"] is True and listed[0]["score"] == 10
    assert c.put(f"/api/exams/{e['id']}/release", json={"released": False}).json()["marks_released"] is False
    assert all(not r["released"] and "score" not in r for r in student.get("/api/me/results").json())
    assert TestClient(app).put(f"/api/exams/{e['id']}/release", json={"released": True}).status_code == 401


def test_twenty_page_pdf_is_graded_and_saved_to_csv():
    import csv as csvlib
    import io
    c = login_teacher()
    e, ids, d = make_exam(c, 10, students=20)
    new_student("50003")                                                  # only some sheets belong to students with accounts
    pngs = [filled_scan(d, ids[i], _tmp, reg=(5, 0, 0, 0, i % 10)) for i in range(20)]   # regs 50000..50009 (10 repeats)
    pdf = pdf_of_images(pngs, os.path.join(_tmp, "twenty.pdf"))
    unreadable = os.path.join(_tmp, "unreadable.png")
    cv2.imwrite(unreadable, np.full((900, 700), 255, np.uint8))
    started = c.post(f"/api/exams/{e['id']}/scan_batch", files=[("files", ("class.pdf", open(pdf, "rb"), "application/pdf")),
                                                                 ("files", ("blank.png", open(unreadable, "rb"), "image/png"))])
    assert started.status_code == 200, started.text
    started = started.json()
    assert started["total"] == 21
    job = wait_job(c, e["id"], started["job"])
    assert len(job["items"]) == 21 and sum(1 for i in job["items"] if i["ok"]) == 20

    exam_csv = list(csvlib.DictReader(io.StringIO(c.get(f"/api/exams/{e['id']}/results.csv").text)))
    assert len(exam_csv) == 20 and all(r["status"] == "checked" and r["score"] == "10.0" for r in exam_csv)
    assert {r["paper_id"] for r in exam_csv} == set(ids)
    named = [r for r in exam_csv if r["student_name"]]
    assert [r["registration_no"] for r in named] == ["50003", "50003"] and named[0]["student_name"] == "s50003"
    r0 = exam_csv[0]
    assert r0["subject"] == "C1" and r0["percentage"] == "100.0"
    assert r0["Q1_correct"] in list("ABCD") and r0["Q1_marked"] == f"{r0['Q1_correct']} (right)" and r0["Q10_marked"].endswith("(right)")
    assert (r0["correct"], r0["wrong"], r0["not_answered"], r0["unclear"]) == ("10", "0", "0", "0")

    batch_csv = list(csvlib.DictReader(io.StringIO(c.get(f"/api/exams/{e['id']}/scan_batch/{started['job']}/results.csv").text)))
    assert len(batch_csv) == 21 and [r["status"] for r in batch_csv].count("NOT SAVED") == 1
    assert batch_csv[0]["page"] == "class.pdf - page 1" and "blank.png" in batch_csv[-1]["page"] and batch_csv[-1]["reason"]
    assert batch_csv[0]["Q1_marked"].endswith("(right)") and batch_csv[0]["correct"] == "10" and batch_csv[-1]["Q1_marked"] == ""

    files = os.listdir(d)                                                 # copies are kept on disk too
    assert "results.csv" in files and f"{started['job']}.csv" in os.listdir(os.path.join(d, "batches"))
    assert TestClient(app).get(f"/api/exams/{e['id']}/results.csv").status_code == 401


def test_csv_marks_right_and_wrong_answers():
    import csv as csvlib
    import io
    c = login_teacher()
    e, ids, d = make_exam(c, 10, students=2)
    layout = json.load(open(os.path.join(d, "layout.json")))
    key = json.load(open(os.path.join(d, "keys.json")))[ids[0]]
    img = preprocess.load_image(os.path.join(d, "sheets", f"{ids[0]}.pdf"))
    px = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    r = int(layout["bubble_radius_mm"] * px * 0.8)

    def dot(b):
        cv2.circle(img, (int(b["cx"] * px), int(b["cy"] * px)), r, 40, -1)

    for col, digit in zip(layout["registration"]["columns"], (6, 6, 6, 6, 6)):
        dot(col[digit])
    for q in layout["questions"]:
        n = q["q"]
        if n == 5:
            continue                                                       # left blank
        if n in (2, 3):
            dot(q["options"]["ABCD"[("ABCD".index(key[n - 1]) + 1) % 4]])   # wrong on purpose
        else:
            dot(q["options"][key[n - 1]])
    path = os.path.join(_tmp, "mixed.png")
    cv2.imwrite(path, img)
    assert c.post(f"/api/exams/{e['id']}/scan", files={"file": ("m.png", open(path, "rb"), "image/png")}).json()["saved"]
    rows = list(csvlib.DictReader(io.StringIO(c.get(f"/api/exams/{e['id']}/results.csv").text)))
    row = next(r for r in rows if r["paper_id"] == ids[0])
    assert (row["correct"], row["wrong"], row["not_answered"]) == ("7", "2", "1") and row["score"] == "7.0"
    assert (row["Q1_correct"], row["Q1_marked"]) == (key[0], f"{key[0]} (right)")
    for q in (2, 3):
        assert row[f"Q{q}_correct"] == key[q - 1]
        marked = row[f"Q{q}_marked"]
        assert marked.endswith(" (wrong)") and marked[0] in "ABCD" and marked[0] != key[q - 1]
    assert (row["Q5_correct"], row["Q5_marked"]) == (key[4], "- (missed)")
    other = next(r for r in rows if r["paper_id"] == ids[1])
    assert other["status"] == "not scanned" and other["Q1_correct"] and other["Q1_marked"] == ""


def test_teacher_sees_full_detail_of_one_sheet():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, release=False)                       # the teacher can look before releasing marks
    new_student("60606")
    scan = filled_scan(d, ids[0], _tmp, reg=(6, 0, 6, 0, 6))
    saved = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()
    rid = saved["result_id"]

    detail = c.get(f"/api/exams/{e['id']}/results/{rid}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["student"] == "s60606" and body["registration"] == "60606" and body["paper_id"] == ids[0]
    assert body["subject"] == "C1" and body["exam"] == "Quiz" and body["score"] == 10
    assert len(body["questions"]) == 10 and body["counts"]["correct"] == 10
    q = body["questions"][0]
    assert q["status"] == "correct" and q["your"] == q["correct"] and len(q["options"]) == 4 and q["text"]

    assert c.get(f"/api/exams/{e['id']}/results/{rid}/sheet.jpg").content[:2] == b"\xff\xd8"
    assert c.get(f"/api/exams/{e['id']}/results/{rid}/sheet.pdf").content[:4] == b"%PDF"
    assert c.get(f"/api/exams/{e['id']}/results/{rid}/clean.pdf").content[:4] == b"%PDF"
    assert c.get(f"/api/exams/{e['id']}/results/{rid}/other.pdf").status_code == 404
    assert c.get(f"/api/exams/{e['id']}/results/999999").status_code == 404

    other = TestClient(app)                                            # another teacher cannot open it
    other.post("/api/register", json={"name": "T9", "email": "t9@t.com", "password": "password123", "role": "teacher"})
    assert other.get(f"/api/exams/{e['id']}/results/{rid}").status_code == 404
    assert other.get(f"/api/exams/{e['id']}/results/{rid}/sheet.jpg").status_code == 404
    assert TestClient(app).get(f"/api/exams/{e['id']}/results/{rid}").status_code == 401
    assert new_student("60607").get(f"/api/exams/{e['id']}/results/{rid}").status_code == 403


def test_sheets_already_processed_are_skipped_with_a_notice():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, students=5)
    regs = [(7, 0, 0, 0, i) for i in range(5)]
    pngs = [filled_scan(d, ids[i], _tmp, reg=regs[i]) for i in range(5)]
    first = pdf_of_images(pngs[:3], os.path.join(_tmp, "three.pdf"))
    whole = pdf_of_images(pngs, os.path.join(_tmp, "whole.pdf"))

    def upload(path, **data):
        r = c.post(f"/api/exams/{e['id']}/scan_batch", files=[("files", (os.path.basename(path), open(path, "rb"), "application/pdf"))], data=data)
        assert r.status_code == 200, r.text
        return wait_job(c, e["id"], r.json()["job"])

    job1 = upload(first)
    assert [i["ok"] for i in job1["items"]] == [True, True, True]

    job2 = upload(whole)                                                      # the 3 done earlier are skipped, 2 are new
    skipped = [i for i in job2["items"] if i.get("skipped")]
    saved = [i for i in job2["items"] if i["ok"]]
    assert len(skipped) == 3 and len(saved) == 2 and len(job2["items"]) == 5
    assert all(i["error"] == "Already processed" and i["score"] == 10 and i["paper_id"] in ids[:3] for i in skipped)
    assert sorted(i["paper_id"] for i in saved) == sorted(ids[3:])
    assert c.get(f"/api/exams/{e['id']}/results").json()["scanned"] == 5

    job3 = upload(whole)                                                      # everything is done now
    assert len(job3["items"]) == 5 and all(i.get("skipped") for i in job3["items"])

    job4 = upload(whole, replace="true")                                      # replacing is an explicit choice
    assert len(job4["items"]) == 5 and all(i["ok"] and not i.get("skipped") for i in job4["items"])


def test_student_upload_of_an_already_processed_sheet():
    c = login_teacher()
    e, ids, d = make_exam(c, 10)
    mine, other = new_student("70001"), new_student("70002")
    scan = filled_scan(d, ids[0], _tmp, reg=(7, 0, 0, 0, 1))
    assert c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()["saved"]

    again = student_upload(mine, scan, "m.png", "image/png")
    assert again.status_code == 200
    body = again.json()
    assert body["already"] is True and body["official"] is True and "already been processed" in body["message"]
    assert mine.get(f"/api/me/results/{body['result_id']}").json()["source"] == "teacher"
    refused = student_upload(other, scan, "m.png", "image/png")
    assert refused.status_code == 403                                      # someone else's sheet is never shown

    # a teacher scan of the same sheet is skipped too (single-file endpoint reports it as not saved)
    single = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()
    assert single["saved"] is False and single["skipped"] is True and single["existing"]["score"] == 10

    # a student's own earlier self-check is also reported as processed, not graded again
    scan2 = filled_scan(d, ids[1], _tmp, reg=(7, 0, 0, 0, 2))
    first = student_upload(other, scan2, "m.png", "image/png").json()
    assert first.get("already") is None and first["official"] is False
    second = student_upload(other, scan2, "m.png", "image/png").json()
    assert second["already"] is True and second["official"] is False and second["result_id"] == first["result_id"]


def test_teacher_can_delete_an_exam_and_students_lose_it():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, students=2)
    token = e["sheets"][0]["qr"].rsplit("/", 1)[1]
    student = new_student("80801")
    scan = filled_scan(d, ids[0], _tmp, reg=(8, 0, 8, 0, 1))
    assert c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()["saved"]
    rid = student.get("/api/me/results").json()[0]["id"]
    assert os.path.isdir(d)

    assert c.delete(f"/api/exams/{e['id']}").status_code == 400                      # no confirmation
    assert c.delete(f"/api/exams/{e['id']}?confirm=nope").status_code == 400
    other = TestClient(app)
    other.post("/api/register", json={"name": "T7", "email": "t7@t.com", "password": "password123", "role": "teacher"})
    assert other.delete(f"/api/exams/{e['id']}?confirm=delete").status_code == 404   # someone else's exam
    assert student.delete(f"/api/exams/{e['id']}?confirm=delete").status_code == 403
    assert TestClient(app).delete(f"/api/exams/{e['id']}?confirm=delete").status_code == 401
    assert c.get(f"/api/exams/{e['id']}").status_code == 200                         # nothing was deleted by those attempts

    done = c.delete(f"/api/exams/{e['id']}?confirm=Delete")                          # case does not matter
    assert done.status_code == 200 and done.json()["deleted"] is True and done.json()["results"] == 1 and done.json()["sheets"] == 2
    assert c.get(f"/api/exams/{e['id']}").status_code == 404
    assert e["id"] not in [x["id"] for x in c.get("/api/exams").json()]
    assert all(r["id"] != rid for r in student.get("/api/me/results").json())       # gone for the student too
    assert student.get(f"/api/me/results/{rid}").status_code == 404
    assert c.get(f"/api/qr/{token}").status_code == 404                              # the printed QR no longer resolves
    assert not os.path.exists(d)                                                     # files removed
    assert c.delete(f"/api/exams/{e['id']}?confirm=delete").status_code == 404


def custom_scan(exam_dir, paper_id, work, reg, marks):
    """A filled sheet where `marks` maps question number -> list of letters to fill (missing = correct answer)."""
    key = json.load(open(os.path.join(exam_dir, "keys.json")))[paper_id]
    layout = json.load(open(os.path.join(exam_dir, "layout.json")))
    img = preprocess.load_image(os.path.join(exam_dir, "sheets", f"{paper_id}.pdf"))
    px = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    r = int(layout["bubble_radius_mm"] * px * 0.8)

    def dot(b):
        cv2.circle(img, (int(b["cx"] * px), int(b["cy"] * px)), r, 40, -1)

    for col, digit in zip(layout["registration"]["columns"], reg):
        dot(col[digit])
    for q in layout["questions"]:
        for letter in marks.get(q["q"], [key[q["q"] - 1]]):
            dot(q["options"][letter])
    path = os.path.join(work, f"custom_{paper_id}.png")
    cv2.imwrite(path, img)
    return path, key


def test_teacher_corrects_a_flagged_sheet_and_resolves_it():
    import csv as csvlib
    import io
    c = login_teacher()
    e, ids, d = make_exam(c, 10, students=2)
    student = new_student("90901")
    path, key = custom_scan(d, ids[0], _tmp, (9, 0, 9, 0, 1), {2: ["A", "B"]})       # Q2 gets two bubbles -> MULTIPLE
    saved = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(path, "rb"), "image/png")}).json()
    rid, base = saved["result_id"], f"/api/exams/{e['id']}/results/{saved['result_id']}"
    first = c.get(base).json()
    q2 = first["questions"][1]
    assert first["flagged"] is True and first["review"] == "review" and first["resolved"] is False and q2["status"] == "invalid"
    before_score = first["score"]
    before_image = c.get(base + "/sheet.jpg").content
    assert c.get(f"/api/exams/{e['id']}/results").json()["rows"][0]["review"] == "review"

    right = key[1]                                                           # the teacher decides the student meant the right option
    fixed = c.patch(base + "/answers", json={"question": 2, "answer": right})
    assert fixed.status_code == 200
    body = fixed.json()
    assert body["questions"][1]["status"] == "correct" and body["questions"][1]["edited"] is True and body["questions"][1]["scanned"] == "MULTIPLE"
    assert body["score"] == before_score + 1
    assert body["resolved"] is True and body["review"] == "resolved" and body["needs_review"] is False    # nothing unclear is left
    assert body["image_redrawn"] is True and c.get(base + "/sheet.jpg").content != before_image
    row = c.get(f"/api/exams/{e['id']}/results").json()
    assert row["rows"][0]["review"] == "resolved" and row["needs_review"] == 0

    rows = list(csvlib.DictReader(io.StringIO(c.get(f"/api/exams/{e['id']}/results.csv").text)))
    mine = next(r for r in rows if r["paper_id"] == ids[0])
    assert mine["review_status"] == "resolved" and mine["needs_review"] == "no" and mine["Q2_marked"] == f"{right} (right)"

    assert c.put(f"/api/exams/{e['id']}/release", json={"released": True}).status_code == 200
    seen = student.get("/api/me/results").json()[0]
    assert seen["score"] == body["score"] and seen["corrected"] is True and seen["needs_review"] is False

    # the toggle works both ways
    assert c.patch(base + "/review", json={"resolved": False}).json()["review"] == "review"
    assert student.get(f"/api/me/results/{rid}").json()["needs_review"] is True
    assert c.patch(base + "/review", json={"resolved": True}).json()["review"] == "resolved"

    # undo puts the scanner's reading back and the sheet needs review again
    undone = c.patch(base + "/answers", json={"question": 2, "answer": "ORIGINAL"}).json()
    assert undone["questions"][1]["status"] == "invalid" and undone["score"] == before_score and undone["review"] == "review"
    assert c.patch(base + "/answers", json={"question": 2, "answer": "ORIGINAL"}).status_code == 409    # nothing left to undo

    # a student's answer can also be marked "not answered"
    blank = c.patch(base + "/answers", json={"question": 2, "answer": "blank"}).json()
    assert blank["questions"][1]["status"] == "blank" and blank["resolved"] is True


def test_editing_answers_is_validated_and_protected():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, students=2)
    path, key = custom_scan(d, ids[0], _tmp, (9, 0, 9, 0, 2), {})                 # every answer correct, nothing flagged
    saved = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(path, "rb"), "image/png")}).json()
    base = f"/api/exams/{e['id']}/results/{saved['result_id']}"
    detail = c.get(base).json()
    assert detail["flagged"] is False and detail["review"] == "" and detail["score"] == 10

    assert c.patch(base + "/review", json={"resolved": True}).status_code == 409        # nothing flagged to resolve
    assert c.patch(base + "/answers", json={"question": 3, "answer": "Z"}).status_code == 422
    assert c.patch(base + "/answers", json={"question": 0, "answer": "A"}).status_code == 422
    assert c.patch(base + "/answers", json={"question": 99, "answer": "A"}).status_code == 422

    wrong = "ABCD"[("ABCD".index(key[0]) + 1) % 4]
    edited = c.patch(base + "/answers", json={"question": 1, "answer": wrong}).json()
    assert edited["score"] == 9 and edited["review"] == "edited" and edited["questions"][0]["status"] == "wrong"
    back = c.patch(base + "/answers", json={"question": 1, "answer": key[0]}).json()        # corrected back to what was read
    assert back["score"] == 10 and back["questions"][0]["edited"] is False and back["review"] == ""

    other = TestClient(app)
    other.post("/api/register", json={"name": "T5", "email": "t5@t.com", "password": "password123", "role": "teacher"})
    assert other.patch(base + "/answers", json={"question": 1, "answer": "A"}).status_code == 404
    assert other.patch(base + "/review", json={"resolved": True}).status_code == 404
    assert new_student("90902").patch(base + "/answers", json={"question": 1, "answer": "A"}).status_code == 403
    assert TestClient(app).patch(base + "/answers", json={"question": 1, "answer": "A"}).status_code == 401
    assert c.get(base).json()["score"] == 10                                              # none of the attempts changed anything


def test_student_uploads_wait_in_line_and_are_private():
    from app import workers
    s = new_student("80801")
    blank = os.path.join(_tmp, "queue_blank.png")
    cv2.imwrite(blank, np.full((600, 400), 255, np.uint8))
    up = lambda: s.post("/api/me/scan", files={"file": ("b.png", open(blank, "rb"), "image/png")})
    with workers.slot():                                                  # the server is busy: nothing can be graded yet
        jobs = [up().json() for _ in range(3)]
        for _ in range(40):                                               # the worker picks up the first one and waits for the slot
            if s.get(f"/api/me/scan/{jobs[0]['job']}").json()["status"] == "working":
                break
            time.sleep(0.25)
        now = [s.get(f"/api/me/scan/{j['job']}").json() for j in jobs]
        assert [j["status"] for j in now] == ["working", "queued", "queued"]
        assert [j.get("position") for j in now[1:]] == [1, 2]             # 1 = next in line
        assert up().status_code == 429                                    # at most 3 waiting per student
        assert new_student("80802").get(f"/api/me/scan/{jobs[0]['job']}").status_code == 404
    for j in jobs:                                                        # once free, every upload is graded in turn
        for _ in range(120):
            j = s.get(f"/api/me/scan/{j['job']}").json()
            if j["status"] == "failed":
                break
            time.sleep(0.25)
        assert j["status"] == "failed" and j["code"] == 422


def test_self_check_before_release_is_saved_but_hidden_and_failures_are_listed():
    c = login_teacher()
    e, ids, d = make_exam(c, 10, release=False)
    s = new_student("80803")
    up = student_upload(s, filled_scan(d, ids[0], _tmp, reg=(8, 0, 8, 0, 3)), "mine.png", "image/png")
    assert up.status_code == 200 and up.json()["released"] is False and "released" in up.json()["message"]
    teacher_rows = c.get(f"/api/exams/{e['id']}/results").json()["rows"]                  # the teacher sees it straight away
    assert any(r["registration"] == "80803" and r["score"] == 10 for r in teacher_rows)
    hidden = s.get("/api/me/results").json()
    assert len(hidden) == 1 and hidden[0]["released"] is False and "score" not in hidden[0]
    assert c.put(f"/api/exams/{e['id']}/release", json={"released": True}).status_code == 200
    assert s.get("/api/me/results").json()[0]["score"] == 10                               # visible once released

    blank = os.path.join(_tmp, "fail_blank.png")
    cv2.imwrite(blank, np.full((600, 400), 255, np.uint8))
    assert student_upload(s, blank, "b.png", "image/png").status_code == 422
    failed = s.get("/api/me/scans").json()                                                   # a failure stays visible
    assert len(failed) == 1 and failed[0]["status"] == "failed" and failed[0]["error"]
    assert new_student("80804").delete(f"/api/me/scans/{failed[0]['job']}").status_code == 404
    assert s.delete(f"/api/me/scans/{failed[0]['job']}").json() == {"ok": True}
    assert s.get("/api/me/scans").json() == []


def test_scan_queue_survives_errors_and_restarts(monkeypatch):
    import app.main as m
    from app.db import ScanJob, SessionLocal
    real, calls = m.claim_next_scan, []

    def flaky():                                                         # the first read of the queue fails
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection dropped")
        return real()

    monkeypatch.setattr(m, "claim_next_scan", flaky)
    m.SCAN_WAKE.set()
    s = new_student("80805")
    blank = os.path.join(_tmp, "q_blank.png")
    cv2.imwrite(blank, np.full((600, 400), 255, np.uint8))
    assert student_upload(s, blank, "b.png", "image/png").status_code == 422             # still processed afterwards

    db = SessionLocal()                                                  # restart: re-queue, or give up after 2 tries
    uid = db.query(ScanJob).filter_by(status="failed").first().user_id
    again, gave_up = ScanJob(user_id=uid, path="/nonexistent", name="x.jpg", status="working", attempts=1), \
        ScanJob(user_id=uid, path="/nonexistent", name="y.jpg", status="working", attempts=2)
    db.add_all([again, gave_up])
    db.commit()
    with m.SCAN_CLAIM:                                                   # keep the live workers away while we look
        m.recover_unfinished_scans()
        db.expire_all()
        assert db.get(ScanJob, again.id).status == "queued" and db.get(ScanJob, gave_up.id).status == "failed"
    db.close()


def test_score_always_matches_the_right_and_wrong_marks():
    """No negative marking (+1 right, 0 otherwise), and the score uses the same key as the green/red marks - even if the
    copy of the key on disk disagrees."""
    c = login_teacher()
    e, ids, d = make_exam(c, 10)
    scan = filled_scan(d, ids[0], _tmp)
    keys = json.load(open(os.path.join(d, "keys.json")))
    keys[ids[0]] = ["D" if k != "D" else "A" for k in keys[ids[0]]]                  # a wrong copy of the key on disk
    json.dump(keys, open(os.path.join(d, "keys.json"), "w"))
    res = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(scan, "rb"), "image/png")}).json()
    assert res["saved"] and res["score"] == 10 and res["counts"]["correct"] == 10
    row = next(r for r in c.get(f"/api/exams/{e['id']}/results").json()["rows"] if r["paper_id"] == ids[0])
    assert row["score"] == row["counts"]["correct"] == 10
    from omr import config as cfg
    assert cfg.SCORE_CORRECT == 1 and cfg.SCORE_INCORRECT == 0                        # wrong answers cost nothing


def test_anti_ai_notice_is_optional_and_scanning_still_works():
    c = login_teacher()
    payload = dict(exam_payload(10, students=2), ai_notice=True)
    r = c.post("/api/exams", json=payload)
    assert r.status_code == 200, r.text
    e = wait_ready(c, r.json()["id"])
    d = os.path.join(os.environ["OMR_EXAMS_DIR"], str(e["id"]))
    for s in e["sheets"]:
        text = "".join(p.get_text() for p in fitz.open(os.path.join(d, "sheets", f"{s['paper_id']}.pdf")))
        assert text.count("not practice") == 5                         # after Q2, Q4, Q6, Q8, Q10
        assert "deskoros.tech" in text and "Deskoros" in text
    pid = e["sheets"][0]["paper_id"]
    res = c.post(f"/api/exams/{e['id']}/scan", files={"file": ("f.png", open(filled_scan(d, pid, _tmp), "rb"), "image/png")}).json()
    assert res["paper_id"] == pid and res["score"] == 10                        # the answer strip is untouched
    _, ids, d2 = make_exam(c, 10)                                               # off by default
    assert "not practice" not in fitz.open(os.path.join(d2, "sheets", f"{ids[0]}.pdf"))[0].get_text()


def test_installable_app_files():
    c = TestClient(app)
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    man = m.json()
    assert man["name"] == "Deskoros" and man["display"] == "standalone" and man["start_url"].startswith("/")
    for icon in man["icons"]:
        r = c.get(icon["src"])
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert any(i.get("purpose") == "maskable" for i in man["icons"]) and any(i["sizes"] == "512x512" for i in man["icons"])
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and sw.headers["cache-control"] == "no-cache"
    assert '"/api/"' in sw.text                                        # live data is excluded from the cache
    assert c.get("/icons/..%2Fsw.js").status_code == 404 and c.get("/icons/nope.png").status_code == 404   # no escaping the folder
    page = c.get("/").text
    assert 'rel="manifest"' in page and "apple-touch-icon" in page and "<title>Deskoros</title>" in page

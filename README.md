# ScanExam

Optical-mark (OMR) grading for schools and colleges. A teacher pastes multiple-choice questions, the app prints a
personal answer sheet for every student (own paper ID and QR code), reads the filled bubbles from a scanner or a phone
camera, and gives each student a clear breakdown of their marks.

- **Teachers:** create exams, print sheets, grade a whole class from one PDF (or page by page with the camera), export
  results to a spreadsheet, release marks when ready.
- **Students:** see marks, every question with the correct answer, and their own marked answer sheet; can also check their
  sheet themselves by scanning it.
- **Stack:** Python 3.10+ / FastAPI, OpenCV, LaTeX (`pdflatex`) for the sheets, one-file React front end, SQLite or PostgreSQL.

> Status: built and tested on synthetic scans and simulated phone photos. Thresholds have **not** been tuned on real
> scanned sheets yet - see [Tuning with real scans](#tuning-with-real-scans).

---

## Contents
1. [Quick start](#quick-start)
2. [Configuration](#configuration)
3. [How it works](#how-it-works)
4. [Using the app](#using-the-app)
5. [Writing questions](#writing-questions)
6. [Results, CSV, QR codes](#results-csv-qr-codes)
7. [Security](#security)
8. [Database](#database)
9. [Docker](#docker)
10. [Deploying](#deploying)
11. [Command-line tools](#command-line-tools)
12. [Project layout](#project-layout)
13. [Testing](#testing)
14. [Tuning with real scans](#tuning-with-real-scans)
15. [Troubleshooting](#troubleshooting)
16. [Known limitations](#known-limitations)

---

## Quick start

Requirements: Python 3.10+, and `pdflatex` with these TeX Live packages: `texlive-latex-base`, `-latex-recommended`,
`-latex-extra`, `-pictures`, `-fonts-recommended` (Ubuntu/Debian names; Docker installs them for you).

```bash
python -m venv venv && source venv/bin/activate      # or: conda create -n omr python=3.11 && conda activate omr
pip install -r requirements.txt
cp .env.example .env                                  # optional, see Configuration
python server.py                                      # http://localhost:8000
```

Create the first teacher by signing up in the browser, or from the command line:

```bash
python create_user.py --role teacher --email me@school.edu --password 'a long password' --name "Ms Rao"
```

**Demo teacher login (local development only):** email `teacher@example.com`, password `Teacher@12345`. This account lives
only in your local `data/platform.db` (not in Git). Change the password or delete the account before any real deployment.

Students sign up themselves with a **5-digit student ID** (the same digits they bubble as their registration number) and
any password.

## Configuration

Settings come from environment variables or a `.env` file next to `server.py` (copy `.env.example`). `.env` is git-ignored.

| Variable | Default | Meaning |
|---|---|---|
| `OMR_HOST` / `OMR_PORT` | `127.0.0.1` / `8000` | Where the server listens (Docker sets `0.0.0.0`). |
| `OMR_PUBLIC_URL` | `http://localhost:8000` | Address printed **inside the QR codes**. Must be reachable from students' phones, e.g. `http://192.168.1.20:8000`. Set it *before* generating sheets. |
| `OMR_TEACHER_INVITE` | empty | If set, people must enter this code to sign up as a teacher. |
| `DATABASE_URL` | empty | Empty = local SQLite file. Otherwise a PostgreSQL URL (e.g. Supabase). See [Database](#database). |
| `OMR_DB` | `data/platform.db` | SQLite file location (used when `DATABASE_URL` is empty). |
| `OMR_EXAMS_DIR` | `output/exams` | Where generated sheet PDFs, scans and CSVs are stored. |
| `OMR_FORMATS_DIR` | `output/formats` | Cached layout/baseline for each sheet format (10 or 15 questions). |

Scanner thresholds and sheet geometry are in `omr/config.py` and `sheet/sheet_config.json`.

## How it works

1. **Paper.** The teacher pastes questions as plain text. They are converted to LaTeX (special characters escaped, `x^2`
   typeset, `$...$` kept as maths) and compiled with `pdflatex` using a Jinja2 template (`sheet/templates/omr_sheet.tex.j2`).
   Each student gets a PDF with: the question paper on the left, and on the right a thick black **frame** holding
   - the **registration number** bubbles (5 digits, filled by the student),
   - the **answer bubbles** (10 or 15 questions, A-D),
   - a **paper ID** (6 digits) that is pre-printed and pre-filled, unique across all exams,
   - a **QR code**.
   A random question subset/order (and optionally option order) is chosen per student; the answer key is stored per paper ID.
2. **Scan.** The scanner finds the black frame (its four corners), uses the solid header bar to detect rotation, warps the
   page to a flat A4, and measures how dark each bubble is (minus a per-bubble baseline for the printed digits).
   Phone photos are first cropped, straightened and freed of shadows (`omr/docscan.py`).
3. **Decide.** Per question: a letter, `BLANK`, `MULTIPLE` (two clear marks) or `UNCERTAIN` (faint mark). Thresholds:
   `MIN_FILL 0.35`, `MIN_GAP 0.15`, `UNCERTAIN_FILL 0.2`. Anything ambiguous is **flagged for review**, never guessed.
4. **Grade.** The paper ID read from the sheet selects the stored key; +1 per correct answer (configurable in `omr/config.py`).
   The marked-up sheet image, a clean scan and the per-question result are saved.

Sheet formats: **10 or 15 questions**, one side. Question text that does not fit beside the answer strip continues
full-width on page 2 (no bubbles there). Only page 1 of each student's paper is scanned.

## Using the app

### Teacher
- **Overview** - your exams and where each stands (sheets printed / checked / waiting for release).
- **New exam** - institution, course, title, optional instructor, max marks, duration, exam date, number of students,
  question order/option shuffling, sheet size, and the questions. **Preview sheet** compiles one sample; **Generate**
  builds every student's PDF in the background.
- **Exam page**
  - *Download all papers (one PDF)* - every student's paper in order (print single-sided), or a `.zip` with one PDF per
    student plus `manifest.csv`.
  - *Release marks* toggle - until it is on, students cannot see marks, cannot self-check, and the QR shows "no preview".
  - *Grade answer sheets* - **Upload PDF / images** (a whole class PDF, one student per page) and/or **Scan a page**
    (live camera; tap repeatedly to add many pages), then **Check**. Progress is shown page by page; unreadable pages are
    listed with the reason. Sheets already checked are **skipped** with a notice; tick *Check again and replace results that
    were already checked* to redo them. A student's self-check is always replaced by the teacher's scan.
  - *Results* - one row per checked sheet. **Click a row** for the full view: every question as printed on that student's
    paper, their answer vs the correct one, and the scanned sheet with coloured bubbles (green correct, red wrong, blue the
    right answer that was missed, orange unclear), plus marked-sheet and clean-scan PDFs.
  - *Fixing flagged answers* - a sheet with a double or faint mark is listed as **needs review**. Open it, click the option the
    student actually chose in each highlighted question (or *Mark as not answered*), and the score and the marked image update at
    once. When no unclear answer is left the review is set to **resolved** automatically; the switch at the top of the sheet
    flips between *needs review* and *resolved* by hand. Every corrected answer shows *edited* and an **Undo** that restores what
    the scanner read. Students see the corrected score and a note; the results table and CSV (`review_status`) show the state.
  - *Delete exam* - type `delete` in the pop-up. Removes the exam, sheets, QR links, all results (students lose them too)
    and the generated files. Refused while sheets are being generated or checked.

### Student
- **Results** - subject, exam, institution, date, paper ID, score, and a full question-by-question review with the marked sheet.
  Results appear only after the teacher has checked the sheet **and** released the marks.
- **Scan sheet** - open the camera (or upload a photo/PDF) of a filled sheet to get the marks immediately. The sheet is
  recognised by its paper ID, its registration number must match the student's ID, and the result is labelled *self-check*.
  If the sheet was already processed, the student is shown that result with a notice instead of being graded again.

### Scanning the QR code on a sheet
The QR is a link `<OMR_PUBLIC_URL>/r/<token>`. The link itself shows nothing: it asks for a login, then opens the result in
the person's own dashboard - a **teacher** who owns the exam lands on that student's result page; a **student** lands on
their own result (only if the sheet carries their registration number and marks are released). Anyone else sees "belongs to
another teacher / student"; an unchecked sheet or unreleased marks show "No preview".

## Writing questions

Paste all questions into one box:

```
What is the capital of France?
A) Berlin
B) Paris
C) Rome
D) Madrid
Answer: B

What is $2^3$?
A) 6
B) 8
C) 9
D) 16
Answer: B
```

- One blank line between questions; options `A) B) C) D)`; the correct one on an `Answer: X` line (or put `*` before it).
- Leading numbers (`1.`), `**bold**` markers, `A.` instead of `A)`, and `Answer: B) text` are all tolerated.
- Maths goes between dollar signs. Commands that could read files or change LaTeX are rejected.
- Up to 1000 questions may be pasted; if more than the sheet holds (10 or 15), each student gets a different random selection.
- The box reports how many questions it found and what is wrong with any it cannot read.
- **AI helper:** under *Questions*, open *Need questions? Copy a ready-made prompt* - fill in topic, count and level, copy the
  prompt into ChatGPT/Gemini/Claude, and paste the reply. Always check AI-written questions and answers before printing.

## Results, CSV, QR codes

- **Download all results (CSV)** - one row per printed sheet (checked or not): student no., paper ID, registration no.,
  student name and login ID (if they have an account), status, score, total, percentage, max marks, correct / wrong /
  not_answered / unclear counts, needs review, flags, source (teacher or student), scan time, institution, subject, exam, date,
  and two columns per question: `Qn_correct` (the correct answer) and `Qn_marked` (what the student marked, with the verdict
  in brackets: `B (right)`, `A (wrong)`, `- (missed)` for a blank, `- (multiple)`, `- (unclear)`).
- **Download this upload (CSV)** - one row per scanned page of the last upload, including unreadable and skipped pages.
- Copies are written to `<exams dir>/<id>/results.csv` and `.../batches/<job>.csv`. Text starting with `=`, `+`, `-` or `@`
  is prefixed with `'` so spreadsheets do not run it as a formula.

## Security

- Passwords are hashed with bcrypt; sessions are random tokens stored hashed, sent as `HttpOnly`, `SameSite=Lax` cookies.
  Serve the site over HTTPS in production.
- Every teacher action checks the login, the teacher role **and** that the exam belongs to that teacher. Students see only
  results for their own registration number, and only after release.
- The QR token (24 random hex characters) cannot be used to log in and reveals nothing without a login. QR lookups are rate limited.
- Question text is compiled in a restricted LaTeX sandbox: no shell escape, no access to files outside the working folder,
  a deny-list for dangerous commands, and a timeout.
- Uploads are size limited; CSV cells are protected against spreadsheet formula injection.
- On PostgreSQL, row-level security is switched on for every table so a public Supabase API key cannot read the data.
- **Not verified:** student registration numbers. The first person to sign up with a number can see that number's results.
  Add a teacher-approved class list if you need proof of identity.

## Database

SQLite (default) is fine for one server and a few hundred students. To use PostgreSQL / Supabase set `DATABASE_URL`:

1. Create a project at supabase.com (free plan: 500 MB database; projects pause after a week without activity).
2. Project -> **Connect** -> **Session pooler** (or Transaction pooler) -> copy the string, insert your password:
   `DATABASE_URL=postgresql://postgres.<project-ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:5432/postgres`
   Passwords containing `@` or other special characters are handled automatically. Do not commit it.
3. Start the app - tables are created automatically.
4. Optional: copy existing data with `python migrate_db.py --to "$DATABASE_URL"` (target must be empty).
5. If it cannot connect, run `python check_db.py`: it checks DNS, the network path and the login step by step. Some
   college/office networks block database ports (5432 / 6543); try another network or run it where the app will be hosted.

Generated PDFs, scan images and CSVs are **files**, not database rows - keep the exams folder on a persistent disk.
The server refuses to start (with a clear message) if `DATABASE_URL` is set but unreachable, instead of silently switching to SQLite.

## Docker

```bash
cp .env.example .env            # set OMR_PUBLIC_URL (and DATABASE_URL if you use PostgreSQL)
docker-compose up -d --build    # or: docker compose up -d --build
docker-compose exec web python create_user.py --role teacher --email me@school.edu --password 'a long password' --name "Ms Rao"
```

Open http://localhost:8000. The image (~1 GB) contains Python, OpenCV and the LaTeX packages. Data lives in the `omr_storage`
volume (`docker-compose down` keeps it, `down -v` deletes it). Update with `git pull && docker-compose up -d --build`. Run the
tests inside the container with `docker-compose exec web python -m pytest -q`. If Docker says "permission denied", add yourself to
the docker group: `sudo usermod -aG docker $USER` and log in again.

**Live camera needs HTTPS.** Browsers allow the in-page camera only on `https://` pages or `localhost`. Over plain
`http://192.168...` the button falls back to the phone's own camera app (the photo is still uploaded). For the live view on phones:
`docker-compose --profile https up -d`, then open `https://<computer address>:8443` and accept the certificate warning
(set `OMR_PUBLIC_URL` to that https address too).

Keep to **one** container/worker: batch-grading progress is held in memory.

## Deploying

The app is one FastAPI process plus files on disk, so it needs a host that runs Docker **and** keeps a persistent disk.
Guidance (check each provider's current docs and prices):
- **Render / VPS:** deploy the Docker image, attach a disk mounted at `/storage` (the image already points the database and
  exams there), set `OMR_PUBLIC_URL`, `OMR_TEACHER_INVITE` and (if used) `DATABASE_URL`, health check `/api/health`. Free plans
  without a disk lose data on restart. Use one instance.
- **Vercel for the front end:** `web/index.html` is a static file, but Vercel cannot run the backend (LaTeX, OpenCV, files).
  Serve `web/` from Vercel and forward `/api/*` to the backend with a rewrite in `vercel.json`:
  `{"outputDirectory": "web", "rewrites": [{"source": "/api/:path*", "destination": "https://YOUR-BACKEND/api/:path*"}, {"source": "/r/:token", "destination": "/index.html"}]}`.
  Large PDF uploads through the proxy may hit Vercel's request-size limit. The simplest setup is to let the backend serve everything.

## Command-line tools

| Command | Purpose |
|---|---|
| `python server.py` | Start the web app. |
| `python create_user.py ...` | Create a teacher or student account. |
| `python check_db.py` | Diagnose the `DATABASE_URL` connection. |
| `python migrate_db.py --to <url>` | Copy all data from SQLite (or another DB) into an empty database. |
| `python sheet/build_sheet.py` | Rebuild the sample sheet (`sheet/omr_sheet.tex`, `data/layout.json`) from `sheet/sheet_config.json` and `sheet/exam.json`; compile with `pdflatex` twice. |
| `python calibrate.py <blank scan>` | Store the blank-sheet baseline (`data/baseline.json`) for the sample sheet. |
| `python scan.py <scan> [--key data/keys.json] [--debug]` | Grade one scan of the sample sheet from the terminal (prints `1-A` lines). |
| `python batch.py <folder>` | Grade a folder of scans of the sample sheet into `output/results/results.csv`. |

## Project layout

```
app/          FastAPI backend: main.py (routes), db.py (models), auth.py, sheets.py (PDF generation), results.py, textconv.py
omr/          scanner: markers, warp, bubbles, grader, pipeline, docscan (photo clean-up), calibration, config.py (thresholds)
sheet/        sheet_config.json, exam.json (sample), build_sheet.py, templates/omr_sheet.tex.j2 (Jinja2 LaTeX template)
web/          index.html - the whole front end (React from a CDN, no build step); responsive, phone friendly
data/         sample layout/baseline/keys; platform.db (git-ignored)
tests/        pytest suite (scanner, platform API, photos, question parser)
scans/ blank_scan/   drop-in folders for the command-line tools (contents git-ignored)
output/       generated files (git-ignored)
Dockerfile  docker-compose.yml  Caddyfile  .env.example  requirements.txt
create_user.py  check_db.py  migrate_db.py  scan.py  batch.py  calibrate.py  server.py
```

Changing the sheet (question counts, option letters, ID digits, spacing, header) is done in `sheet/sheet_config.json`; the
scanner layout is regenerated from the same numbers, so the printed sheet and the reader cannot drift apart.

## Testing

```bash
python -m pytest -q          # ~3 minutes; builds real PDFs with pdflatex
```

The suite covers marker detection and warping, bubble decisions, synthetic scans (rotated, shifted, scaled, noisy, shadowed),
simulated phone photos, the question parser, and the whole platform API (accounts, sheet generation, batch and single grading,
skip/replace rules, release toggle, QR logic, CSV, student access rules, delete). It runs on SQLite with temporary folders and
does not touch your data; make sure `DATABASE_URL` is not exported in your shell. The platform tests also pass against a real
PostgreSQL server.

## Tuning with real scans

Thresholds were tuned on synthetic data. To tune on real sheets, print a few, fill them with your usual pens/pencils (include
erasures and double marks), scan at 200-300 DPI, then adjust `omr/config.py`:

- Real marks read below `MIN_FILL` (0.35) -> lower `MIN_FILL` / `UNCERTAIN_FILL`.
- Erasures or stray dots read as marks -> raise `MIN_FILL`.
- Too many `MULTIPLE` on heavy single marks -> lower `MIN_GAP`.
- Uneven scanner background -> try `BINARIZE_METHOD = "otsu"` or change `ADAPTIVE_BLOCK_SIZE` / `ADAPTIVE_C`.
- Frame not found -> check the `FRAME_*` limits and that the page is not cropped.
- Every bubble offset by the same amount -> the printer did not print at 100% scale.

Use `python scan.py <scan> --debug` (sample sheet) and open `output/debug/*_debug.jpg`: green = marked, red = multiple/uncertain,
grey = unmarked, with the fill ratio of each option.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Alignment frame not found" | The black frame is cropped or hidden. Re-scan with the whole sheet in view. Page 2 (questions only) always shows this - upload page 1 only. |
| Camera does not open live | Page is not HTTPS (see [Docker](#docker)); the phone camera app fallback is offered instead. |
| "Cannot open the database" on start | `DATABASE_URL` is wrong or blocked - run `python check_db.py`, or empty it to use SQLite. |
| LaTeX error in preview | Message shows the problem; usually a stray `$` or an unsupported command in the question text. |
| Student sees nothing | The sheet is not scanned yet, or the teacher has not turned on *Release marks*. |
| "Permission denied" from Docker | `sudo usermod -aG docker $USER`, then log in again. |

## Known limitations

- Only 10- and 15-question single-side sheets; four options (A-D); one correct answer per question.
- Phone photos are supported but tested only on simulated photos: keep the whole sheet in view, in good light, on a darker surface.
- Only page 1 of each student's paper is scanned.
- Student registration numbers are not verified; a teacher-approved class list is not implemented.
- Batch-grading progress is in memory (one server process); files need a persistent disk even when the database is hosted.
- Scoring is +1 per correct answer with no negative marking or per-question marks (change `omr/config.py` for a global value).
- No password reset, email notifications, or multi-teacher sharing of an exam.

# TASK: Build an OMR (bubble sheet) scanner in Python + OpenCV

You are building a complete OMR grading system for a **fixed template** MCQ sheet that is scanned on a flatbed/feeder scanner. Work step by step, test each stage, and do not skip the tests.

## 0. Project folder (you are working inside it)

The project root is the folder that contains this file. Everything you need is already here. Read these files first, before writing any code:

| Path | What it is |
|---|---|
| `agent_prompt.md` | This file (the full instructions). |
| `build_sheet.py` | Generates `omr_sheet.tex` and `layout.json` from ONE config. Single source of truth for the layout. If the layout must change, change it here and regenerate. Do not redesign it. |
| `layout.json` | All coordinates in **millimetres**, origin = top-left of the A4 page, x right, y down. Markers, ID bubbles, question bubbles. Load this for ALL geometry. |
| `omr_sheet.tex` / `omr_sheet.pdf` | The printable sheet (compile the .tex with `pdflatex` twice). The PDF is used to make synthetic test images. |
| `keys.json` | Answer keys by paper ID, e.g. `{"1001": ["A","C", ... 25 items]}`. Use it for grading. I will replace it with real keys later. |
| `scans/` | Put real scanned sheets here (may be empty at first). Batch mode reads this folder. |
| `blank_scan/` | A scan of ONE blank printed sheet, used by `calibrate.py` (may be empty at first; if empty, use the relative fallback and tell me). |

If any file above is missing or unreadable, stop and tell me instead of inventing it.

## 1. Sheet specification

- Paper: A4, 210 x 297 mm. Printed at 100% scale (no "fit to page").
- **4 solid black alignment squares**, centres 12 mm from the page edges:
  - TL: 12 x 12 mm (deliberately BIGGER, used to detect orientation)
  - TR, BR, BL: 8 x 8 mm
  - Marker centres are in `layout.json -> markers`.
- **Registration number**: 5 columns, digits 0-9 per column (bubbles), plus 5 handwritten boxes above (human readable only, NEVER scanned).
- **Paper ID**: 4 columns, digits 0-9 per column, plus 4 handwritten boxes above (same rule).
- **Questions**: 25 questions, options A-D, two columns (Q1-13 left, Q14-25 right).
- Bubble radius: 2.2 mm. The registration/paper-ID bubbles have a small printed digit inside; the question bubbles are empty.
- All bubble centres are in `layout.json` (`registration.columns[c][digit]`, `paper_id.columns[c][digit]`, `questions[i].options[A..D]`).

## 2. Deliverables

Create this project structure:

```
(project root = this folder)
  build_sheet.py          (given)
  layout.json             (given)
  omr_sheet.tex/.pdf      (given)
  keys.json               (given, example)
  scans/  blank_scan/     (given, input folders)
  omr/                    (package you create)
    __init__.py
    preprocess.py         # load image, grayscale, threshold
    markers.py            # find 4 markers, order them, detect rotation
    warp.py               # perspective warp to canonical size
    bubbles.py            # fill-ratio measurement + decision logic
    grader.py             # answer key lookup + scoring
    pipeline.py           # ties everything together
  scan.py                 # CLI: python scan.py scan.png --key keys.json
  batch.py                # CLI: process a folder -> results.csv
  make_test_data.py       # synthetic test generator (see section 6)
  tests/                  # pytest tests
  README.md
```

Language: Python 3.10+. Dependencies: `opencv-python`, `numpy`, `pdf2image` or `PyMuPDF` (only if PDFs need to be read), `pytest`. No deep learning.

## 3. Pipeline (implement exactly this order)

### 3.1 Load
- Accept PNG/JPG/TIFF, and PDF (first page) rendered at 200 DPI.
- Convert to grayscale. Do NOT resize yet.

### 3.2 Find the 4 markers
1. Blur lightly (Gaussian 5x5), then Otsu inverted threshold so ink = 255.
2. `cv2.findContours` with `RETR_EXTERNAL`.
3. Keep a contour only if ALL are true:
   - area within a sane range relative to expected marker size (compute expected px area from image DPI estimate: image width / 210 mm)
   - bounding-box aspect ratio between 0.8 and 1.25
   - `contourArea / boundingRectArea >= 0.85` (solid, filled)
   - located in the outer ~15% band of the image (near a corner)
4. Compute each centroid with `cv2.moments`.
5. Pick the best candidate nearest each image corner.
6. **Orientation**: the largest of the 4 markers is TL. If the biggest one is not in the top-left, rotate the image by 90/180/270 degrees so it is, then re-run marker detection. This handles upside-down or sideways feeding.
7. If fewer than 4 valid markers are found, raise `MarkerError` with a clear message. Never guess.

### 3.3 Warp
- Canonical size: A4 at 200 DPI = 1654 x 2339 px. `px = mm * 200 / 25.4`.
- Source points = the 4 detected marker centres (TL, TR, BR, BL).
- Destination points = the marker centres from `layout.json` converted to px (NOT the image corners, the markers are inset 12 mm).
- `cv2.getPerspectiveTransform` + `cv2.warpPerspective`.
- Save the warped image in debug mode.

### 3.4 Binarise for bubble reading
- Use the warped grayscale. Apply a small Gaussian blur, then adaptive threshold (`ADAPTIVE_THRESH_GAUSSIAN_C`, block size ~ 35, C ~ 10) inverted so ink = 255. Also compute an Otsu version and use whichever gives cleaner separation (make this switchable via config).
- Erase the printed bubble outline influence: measure fill only inside a circle of radius ~ 0.75 x bubble radius (about 1.65 mm), so the printed ring itself is not counted as ink.

### 3.5 Fill measurement
For each bubble: `fill = nonzero_pixels_in_mask / mask_area` (0..1).

To handle the digits printed inside ID bubbles: measure the **baseline** fill of each ID bubble on a blank sheet (add a `calibrate.py` that scans a blank printed sheet and stores per-bubble baseline in `baseline.json`). Then use `fill - baseline` for ID bubbles. If no baseline exists, fall back to comparing bubbles within the same column relatively.

### 3.6 Decision logic (very important)
Use RELATIVE + ABSOLUTE thresholds, all configurable in `config.py`:

For each question (4 options):
- `MIN_FILL = 0.35` : below this, the bubble is "not marked".
- `MIN_GAP = 0.15` : the best bubble must exceed the second best by this much to be a single clear answer.
- Result states: `ANSWER(letter)`, `BLANK`, `MULTIPLE`, `UNCERTAIN`.
  - BLANK: all fills < MIN_FILL.
  - MULTIPLE: two or more fills >= MIN_FILL and gap < MIN_GAP.
  - UNCERTAIN: best fill between 0.2 and MIN_FILL (light mark), flag for manual review.
  - ANSWER: exactly one clear winner.

For registration number and paper ID (per column, 10 options):
- Exactly one bubble must be marked. Otherwise the column is `?` and the whole ID is flagged `INVALID`.
- Output ID as a string like `"12345"`.

### 3.7 Grading
- Look up the answer key by paper ID from `keys.json`.
- Score: +1 for correct, 0 for wrong/blank/multiple (make marks per correct and negative marking configurable).
- If paper ID is unknown/invalid, do not crash: return the raw answers and set `needs_review = true`.

### 3.8 Output
Return and save JSON per sheet:

```json
{
  "file": "scan_001.png",
  "registration": "12345",
  "paper_id": "1001",
  "answers": {"1": "A", "2": "BLANK", "3": "MULTIPLE", "...": "..."},
  "score": 19,
  "total": 25,
  "needs_review": false,
  "flags": ["Q3 multiple marks"]
}
```

Batch mode writes `results.csv` plus one debug image per sheet.

## 4. Debug output (required)

With `--debug`, save an image of the warped sheet with:
- green circle on bubbles detected as marked, red on multiple/uncertain, thin grey on the rest
- the fill ratio printed next to each question row
- markers outlined
This is how we will tune thresholds, so make it good.

## 5. Config

Put every tunable in `omr/config.py`: DPI, canonical size, thresholds, block size, sampling radius factor, marker area limits, marker band. No magic numbers scattered in code.

## 6. Testing (do not skip)

1. **Synthetic tests** (`make_test_data.py`): render `omr_sheet.pdf` to an image, then programmatically fill chosen bubbles using `layout.json` (draw dark filled circles at chosen coordinates), then produce degraded variants:
   - rotate +-1.5 degrees, shift 3 mm, scale 98% and 102%
   - rotate 180 degrees and 90 degrees (orientation test)
   - add Gaussian noise, slight blur, uneven lighting gradient
   - light partial fills (50% radius, low darkness)
   - double marks on some questions, blank questions
   Assert that the pipeline recovers the known answers exactly for clean and mildly degraded variants, and correctly flags MULTIPLE/BLANK/UNCERTAIN.
2. **Unit tests** for marker detection (rejects a page with only 3 markers), warp accuracy (marker centres land within 1 px of target after warp), and decision logic.
3. **Real scans**: after synthetic tests pass, the README must tell me how to run on real scans and how to tune thresholds using debug images. Ask me for 20-30 real filled sheets to tune on.

## 7. Rules

- Never silently guess. Every ambiguous case must appear in `flags` and set `needs_review`.
- No hardcoded pixel coordinates. Everything comes from `layout.json` via mm-to-px conversion.
- Keep functions small and typed, add docstrings.
- After finishing, run all tests and paste the results, then summarize known limitations (for example: phone photos are NOT supported, only scanner images).

## 8. Order of work

1. Regenerate sheet: `python build_sheet.py && pdflatex omr_sheet.tex && pdflatex omr_sheet.tex`
2. Marker detection + warp, with tests.
3. Bubble measurement + decision logic, with tests.
4. Pipeline + CLI + debug overlay.
5. Synthetic data generator + full test run.
6. Batch mode, CSV, README.
Stop and report after each step.

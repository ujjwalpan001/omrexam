"""Student-facing results: review table and the annotated image of the filled answer sheet."""
import json
import os
from typing import Dict, List

import cv2
import numpy as np

from omr import config

GREEN, RED, BLUE, ORANGE = (0, 150, 0), (0, 0, 210), (200, 110, 0), (0, 140, 255)     # BGR


def status_of(answer: str, correct: str) -> str:
    if answer == correct:
        return "correct"
    if answer == "BLANK" or answer is None:
        return "blank"
    if answer in ("MULTIPLE", "UNCERTAIN"):
        return "invalid"
    return "wrong"


def rescore(answers: Dict[str, str], key: List[str]) -> float:
    """Same rule as the scanner's grader: +SCORE_CORRECT per right answer, SCORE_INCORRECT for a wrong letter, 0 otherwise."""
    score = 0.0
    for i, correct in enumerate(key, 1):
        mine = answers.get(str(i), "BLANK")
        if mine == correct:
            score += config.SCORE_CORRECT
        elif mine not in ("BLANK", "MULTIPLE", "UNCERTAIN"):
            score += config.SCORE_INCORRECT
    return score


def review_state(r) -> str:
    """'review' (flagged, not yet handled), 'resolved' (flagged and handled), 'edited' (corrected, nothing flagged) or ''."""
    if r.needs_review:
        return "resolved" if r.resolved else "review"
    return "edited" if r.original_answers else ""


def render_inputs_paths(image_path: str) -> tuple:
    base = image_path[:-4]
    return base + "_page.jpg", base + "_fills.json"


def save_render_inputs(warped: np.ndarray, fills: Dict, image_path: str) -> None:
    """Keep the straightened page and the raw fill ratios so the marked image can be redrawn after a correction."""
    page_path, fills_path = render_inputs_paths(image_path)
    cv2.imwrite(page_path, warped, [cv2.IMWRITE_JPEG_QUALITY, 90])
    with open(fills_path, "w") as f:
        json.dump(fills, f)


def rerender(image_path: str, layout: dict, answers: Dict[str, str], key: List[str]) -> bool:
    """Redraw the marked sheet for corrected answers. False when the inputs were not kept (scanned before this feature)."""
    page_path, fills_path = render_inputs_paths(image_path)
    if not (os.path.exists(page_path) and os.path.exists(fills_path)):
        return False
    page = cv2.imread(page_path, cv2.IMREAD_GRAYSCALE)
    with open(fills_path) as f:
        fills = json.load(f)
    render_student_sheet(page, layout, fills, answers, key, image_path)
    return True


def review_items(exam, sheet, answers: Dict[str, str], original: Dict[str, str] = None) -> List[dict]:
    """Every question as it appeared on this student's paper (their own order and option order)."""
    original = original or {}
    items = []
    for i, qi in enumerate(sheet.question_order):
        q = exam.questions[qi]
        options = [q.options[j] for j in sheet.option_orders[i]]
        correct, mine = sheet.key[i], answers.get(str(i + 1), "BLANK")
        items.append(dict(number=i + 1, text=q.text, options=options, your=mine, correct=correct,
                          status=status_of(mine, correct), edited=str(i + 1) in original, scanned=original.get(str(i + 1))))
    return items


def render_student_sheet(warped: np.ndarray, layout: dict, fills: Dict, answers: Dict[str, str], key: List[str], out_path: str) -> None:
    """The straightened scan with each answer marked: green = correct, red = wrong, blue = the right option that was missed,
    orange = several / unclear marks."""
    img = cv2.cvtColor(warped, cv2.COLOR_GRAY2BGR)
    px = config.CANONICAL_WIDTH / layout["page_mm"]["w"]
    r = int(layout["bubble_radius_mm"] * px) + 4
    label_x = int(layout["frame"]["x0"] * px) - 190
    for q in layout["questions"]:
        n, correct, mine = q["q"], key[q["q"] - 1], answers.get(str(q["q"]), "BLANK")
        row_fills = fills["questions"].get(str(n), {})
        for letter, o in q["options"].items():
            c = (int(o["cx"] * px), int(o["cy"] * px))
            if mine in ("MULTIPLE", "UNCERTAIN"):
                if row_fills.get(letter, 0) >= config.UNCERTAIN_FILL:
                    cv2.circle(img, c, r, ORANGE, 4)
            elif letter == mine:
                cv2.circle(img, c, r, GREEN if letter == correct else RED, 4)
            elif letter == correct:
                cv2.circle(img, c, r, BLUE, 3)
        cy = int(q["options"]["A"]["cy"] * px) + 6
        text, colour = {"correct": ("Correct", GREEN), "wrong": ("Wrong", RED), "blank": ("Not answered", BLUE),
                        "invalid": ("Unclear", ORANGE)}[status_of(mine, correct)]
        cv2.putText(img, f"Q{n}: {text}", (label_x, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, img, [cv2.IMWRITE_JPEG_QUALITY, 85])


def jpg_to_pdf(jpg_path: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, filename=jpg_path)
    return doc.tobytes()


def clean_path(marked_image_path: str) -> str:
    """Where the clean (unmarked) scan sits next to the marked sheet image."""
    return marked_image_path.replace(".jpg", "_clean.jpg")


def write_jpeg(img: np.ndarray, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 88])

"""Find the alignment frame (4 outer corners) and orient the page so the header bar is on top."""
import json
from typing import Dict, Tuple

import cv2
import numpy as np

from . import config
from .exceptions import MarkerError

Corners = Dict[str, Tuple[float, float]]


def order_corners(pts: np.ndarray) -> Corners:
    """Name 4 points TL/TR/BR/BL from their geometry."""
    s = pts.sum(axis=1)
    d = pts[:, 0] - pts[:, 1]
    return {"TL": tuple(pts[np.argmin(s)]), "BR": tuple(pts[np.argmax(s)]),
            "TR": tuple(pts[np.argmax(d)]), "BL": tuple(pts[np.argmin(d)])}


def find_markers(image: np.ndarray, layout: dict) -> Corners:
    """Return the outer corners of the frame in pixel coordinates, or raise MarkerError."""
    blurred = cv2.GaussianBlur(image, (5, 5), 0)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    fr = layout["frame"]
    fw, fh = fr["x1"] - fr["x0"], fr["y1"] - fr["y0"]
    px_per_mm = max(image.shape) / layout["page_mm"]["h"]
    expected_area = fw * fh * px_per_mm ** 2
    expected_aspect = fh / fw

    best, best_area = None, 0.0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (config.FRAME_AREA_MIN_RATIO * expected_area <= area <= config.FRAME_AREA_MAX_RATIO * expected_area):
            continue
        rect = cv2.minAreaRect(cnt)
        (rw, rh) = rect[1]
        if rw == 0 or rh == 0 or area / (rw * rh) < config.FRAME_RECT_FILL_MIN:
            continue
        aspect = max(rw, rh) / min(rw, rh)
        if abs(aspect - expected_aspect) / expected_aspect > config.FRAME_ASPECT_TOLERANCE:
            continue
        if area > best_area:
            best, best_area = rect, area
    if best is None:
        raise MarkerError("Alignment frame not found (missing, cropped, or wrong size).")
    return order_corners(cv2.boxPoints(best))


def header_ink(image: np.ndarray, corners: Corners, layout: dict) -> float:
    """Warp the frame to canonical size and return the dark fraction of the header bar region."""
    from .warp import warp_image
    warped = warp_image(image, corners, layout)
    px = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    fr, m = layout["frame"], 0.8 * layout["frame"]["stroke"]
    x0, x1 = int((fr["x0"] + m) * px), int((fr["x1"] - m) * px)
    y0, y1 = int((fr["y0"] + m) * px), int((fr["y0"] + fr["header_h"] - 0.5) * px)
    region = warped[y0:y1, x0:x1]
    return float((region < 128).mean())


def detect_and_orient_markers(image: np.ndarray, layout: dict) -> Tuple[np.ndarray, Corners]:
    """Try the 4 page rotations; keep the one where the solid header bar is at the top."""
    curr = image
    last_error: Exception = MarkerError("Could not orient image correctly after 4 rotations.")
    for _ in range(4):
        try:
            corners = find_markers(curr, layout)
            if header_ink(curr, corners, layout) >= config.HEADER_INK_MIN:
                return curr, corners
        except MarkerError as e:
            last_error = e
        curr = cv2.rotate(curr, cv2.ROTATE_90_COUNTERCLOCKWISE)
    raise last_error


def load_layout(layout_path: str) -> dict:
    with open(layout_path) as f:
        return json.load(f)

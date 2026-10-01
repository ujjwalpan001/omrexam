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


def page_quad_from_frame(gray: np.ndarray, layout: dict) -> np.ndarray:
    """Corners of the whole page in a photo, worked out from the thick black frame found anywhere in it.

    Used when the paper itself cannot be told apart from a busy background (patterned cloth, bright table): the frame is
    solid black on white paper, so it stands out on any background. Returns TL/TR/BR/BL of the page, header bar on top."""
    h, w = gray.shape
    scale = min(1.0, 1600.0 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    block = int(max(small.shape) * 0.06) | 1                                # wider than the frame stroke
    dark = cv2.adaptiveThreshold(cv2.GaussianBlur(small, (5, 5), 0), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, block, 12)
    contours, _ = cv2.findContours(dark, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    fr, page = layout["frame"], layout["page_mm"]
    fw, fh = fr["x1"] - fr["x0"], fr["y1"] - fr["y0"]
    frame_mm = np.float32([[fr["x0"], fr["y0"]], [fr["x1"], fr["y0"]], [fr["x1"], fr["y1"]], [fr["x0"], fr["y1"]]])
    page_mm = np.float32([[[0, 0]], [[page["w"], 0]], [[page["w"], page["h"]]], [[0, page["h"]]]])
    px = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    best, best_area = None, 0.0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 0.01 * small.shape[0] * small.shape[1] or area <= best_area:
            continue
        hull = cv2.convexHull(cnt)
        quad = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
        if len(quad) != 4 or area / max(cv2.contourArea(quad), 1) < 0.9:
            continue
        q = quad.reshape(4, 2).astype(np.float32) / scale
        sides = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
        long_, short = (sides[0] + sides[2]) / 2, (sides[1] + sides[3]) / 2
        if long_ < short:
            long_, short = short, long_
        if abs(long_ / short - fh / fw) / (fh / fw) > 0.3:                  # the frame is a tall, narrow rectangle
            continue
        o = order_corners(q)
        pts = np.float32([o["TL"], o["TR"], o["BR"], o["BL"]])
        for turn in range(4):                                                 # which side is the header bar?
            corners = np.roll(pts, -turn, axis=0)
            if np.linalg.norm(corners[1] - corners[0]) > np.linalg.norm(corners[3] - corners[0]):
                continue                                                      # top edge must be a short side
            frame_px = frame_mm * px
            H = cv2.getPerspectiveTransform(frame_px, corners)
            canon = cv2.warpPerspective(gray, np.linalg.inv(H), (config.CANONICAL_WIDTH, config.CANONICAL_HEIGHT),
                                        borderValue=255)
            m = 0.8 * fr["stroke"]
            bar = canon[int((fr["y0"] + m) * px):int((fr["y0"] + fr["header_h"] - 0.5) * px),
                        int((fr["x0"] + m) * px):int((fr["x1"] - m) * px)]
            if bar.size and (bar < cv2.threshold(canon, 0, 255, cv2.THRESH_OTSU)[0]).mean() >= config.HEADER_INK_MIN:
                best = cv2.perspectiveTransform(page_mm * px, H).reshape(4, 2)
                best_area = area
                break
    if best is None:
        raise MarkerError("Alignment frame not found (missing, cropped, or wrong size).")
    return best


def load_layout(layout_path: str) -> dict:
    with open(layout_path) as f:
        return json.load(f)

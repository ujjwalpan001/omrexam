"""CamScanner-style document cleanup: find the page in a photo, straighten it, remove shadows, whiten the paper."""
import os
from typing import Optional

import cv2
import numpy as np

from . import config

MAX_SIDE = 3200          # photos are shrunk to this before processing (speed); the output is A4 at 200 DPI
MIN_PAGE_AREA = 0.20     # the page must cover at least this fraction of the photo
# Estimate the paper brightness (for shadow removal) on a 1/4-size copy: ~10x faster, very slightly different pixels.
# Off by default until compare_shadows.py has confirmed identical answers on real photos (env OMR_FAST_SHADOWS=1).
FAST_SHADOWS = os.environ.get("OMR_FAST_SHADOWS", "").strip().lower() in ("1", "true", "yes", "on")


def order_quad(pts: np.ndarray) -> np.ndarray:
    """Return 4 points as TL, TR, BR, BL."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s, d = pts.sum(axis=1), pts[:, 0] - pts[:, 1]
    return np.array([pts[np.argmin(s)], pts[np.argmax(d)], pts[np.argmax(s)], pts[np.argmin(d)]], dtype=np.float32)


def _quad_from_contours(contours, area_min: float) -> Optional[np.ndarray]:
    best, best_area = None, 0.0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < area_min or area <= best_area:
            continue
        hull = cv2.convexHull(cnt)
        approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
        quad = approx.reshape(-1, 2) if len(approx) == 4 else cv2.boxPoints(cv2.minAreaRect(hull))
        if cv2.contourArea(hull) > 0 and cv2.contourArea(quad.astype(np.float32)) / cv2.contourArea(hull) > 0.85:
            best, best_area = quad, area
    return best


def find_page_quad(gray: np.ndarray) -> Optional[np.ndarray]:
    """Corners of the sheet of paper in a photo (in the coordinates of `gray`), or None if it fills the whole image."""
    h, w = gray.shape
    scale = 900.0 / max(h, w)
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    blur = cv2.GaussianBlur(small, (9, 9), 0)
    area_min = MIN_PAGE_AREA * small.shape[0] * small.shape[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))

    candidates = []
    _, bright = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)          # paper is the bright region
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel)                              # close the black frame / text
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates.append(_quad_from_contours(contours, area_min))
    edges = cv2.dilate(cv2.Canny(blur, 30, 100), kernel)                                    # fallback: outline by edges
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates.append(_quad_from_contours(contours, area_min))

    quads = [q for q in candidates if q is not None]
    if not quads:
        return None
    quad = max(quads, key=lambda q: cv2.contourArea(q.astype(np.float32)))
    quad = order_quad(quad / scale)
    centre = quad.mean(axis=0)
    quad = centre + (quad - centre) * 0.988                 # stay just inside the paper edge (no table sliver)
    if cv2.contourArea(quad) > 0.97 * h * w:            # already a full-page scan
        return None
    return quad


def straighten(gray: np.ndarray, quad: Optional[np.ndarray]) -> np.ndarray:
    """Warp the page to portrait A4 (canonical size). With no quad the image is just resized."""
    size = (config.CANONICAL_WIDTH, config.CANONICAL_HEIGHT)
    if quad is None:
        if gray.shape[1] > gray.shape[0]:
            gray = cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
        return cv2.resize(gray, size, interpolation=cv2.INTER_AREA if gray.shape[1] > size[0] else cv2.INTER_CUBIC)
    tl, tr, br, bl = quad
    if (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) > (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)):
        quad = np.roll(quad, -1, axis=0)                 # landscape photo of a portrait page: make the long edges vertical
    dst = np.array([[0, 0], [size[0] - 1, 0], [size[0] - 1, size[1] - 1], [0, size[1] - 1]], dtype=np.float32)
    page = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(quad, dst), size, flags=cv2.INTER_CUBIC, borderValue=255)
    m = 14                                                            # hide the sliver of table at the paper edge
    page[:m], page[-m:], page[:, :m], page[:, -m:] = 255, 255, 255, 255
    return page


def remove_shadows(gray: np.ndarray) -> np.ndarray:
    """Divide by the estimated paper brightness so uneven light and shadows disappear and the paper turns white.

    The estimate uses a window wider than the thick black frame/header bar, so those stay solid black."""
    window = int(max(gray.shape) * 0.045) | 1                       # ~13 mm on the canonical page
    f = 4 if FAST_SHADOWS else 1                                    # work on a 1/f-size copy: shadows are smooth
    small = cv2.resize(gray, None, fx=1 / f, fy=1 / f, interpolation=cv2.INTER_AREA) if f > 1 else gray
    w = max(3, (window // f) | 1)
    # closing (dilate then erode) removes dark print but keeps the outline of a shadow, so no halo at its edge
    background = cv2.morphologyEx(small, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (w, w)))
    background = cv2.GaussianBlur(cv2.medianBlur(background, max(3, (31 // f) | 1)), (0, 0), 8 / f)
    if f > 1:
        background = cv2.resize(background, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    flat = cv2.divide(gray, np.maximum(background, 1), scale=255)
    black, white = np.percentile(flat, 1), np.percentile(flat, 60)                          # paper is the majority of pixels
    return np.clip((flat.astype(np.float32) - black) * 255.0 / max(white - black, 1), 0, 255).astype(np.uint8)


def sharpen(gray: np.ndarray) -> np.ndarray:
    return cv2.addWeighted(gray, 1.6, cv2.GaussianBlur(gray, (0, 0), 2.0), -0.6, 0)


def to_bw(gray: np.ndarray) -> np.ndarray:
    """Crisp black-and-white, like the scanner-app 'B&W' filter."""
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 14)


def clean_page(gray: np.ndarray, mode: str = "gray") -> np.ndarray:
    """Straighten an already-cropped page and clean it. mode: 'gray' (natural) or 'bw' (crisp black/white)."""
    flat = sharpen(remove_shadows(gray))
    return to_bw(flat) if mode == "bw" else flat


def prepare_photo(img: np.ndarray, quad: Optional[np.ndarray] = None) -> np.ndarray:
    """Photo -> straight, shadow-free A4 page at canonical size (used before reading the bubbles).
    `quad` = the page corners when already known (TL, TR, BR, BL in `img` pixels); otherwise the paper is looked for."""
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if max(gray.shape) > MAX_SIDE:
        f = MAX_SIDE / max(gray.shape)
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        quad = None if quad is None else quad * f
    if quad is None:
        page = remove_shadows(straighten(gray, find_page_quad(gray)))
    else:
        size = (config.CANONICAL_WIDTH, config.CANONICAL_HEIGHT)
        dst = np.array([[0, 0], [size[0] - 1, 0], [size[0] - 1, size[1] - 1], [0, size[1] - 1]], dtype=np.float32)
        page = remove_shadows(cv2.warpPerspective(gray, cv2.getPerspectiveTransform(np.float32(quad), dst), size,
                                                  flags=cv2.INTER_CUBIC, borderValue=255))
    m = 12
    page[:m], page[-m:], page[:, :m], page[:, -m:] = 255, 255, 255, 255            # clean white margin
    return page


def scan_document(img: np.ndarray, mode: str = "gray") -> np.ndarray:
    """The whole CamScanner pipeline for a photo: find page, straighten, clean."""
    page = prepare_photo(img)
    page = sharpen(page)
    return to_bw(page) if mode == "bw" else page

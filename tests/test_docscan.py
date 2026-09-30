"""Phone-photo handling: page detection, shadow removal, and reading the bubbles from a photo."""
import json

import cv2
import numpy as np
import pytest

from omr import config, docscan, preprocess
from omr.pipeline import process_scan_full

LAYOUT = json.load(open(config.LAYOUT_PATH))
PX = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
DST = np.float32([[1350, 180], [2650, 300], [2750, 2750], [1200, 2700]])       # where the page corners land in the photo


def filled_sheet():
    sheet = preprocess.load_image(config.SHEET_PDF_PATH)
    r = int(LAYOUT["bubble_radius_mm"] * PX * 0.8)
    for col, d in zip(LAYOUT["registration"]["columns"], [1, 2, 3, 4, 5]):
        cv2.circle(sheet, (int(col[d]["cx"] * PX), int(col[d]["cy"] * PX)), r, 40, -1)
    for q in LAYOUT["questions"]:
        b = q["options"]["ABCD"[q["q"] % 4]]
        cv2.circle(sheet, (int(b["cx"] * PX), int(b["cy"] * PX)), r, 40, -1)
    return sheet


def photo_of(sheet, shadow=True, rotate=0):
    """The sheet on a brown table, tilted, with a soft shadow, blur and noise."""
    H, W = 3000, 4000
    rng = np.random.default_rng(1)
    table = (np.full((H, W, 3), (60, 90, 130), np.uint8) + rng.normal(0, 8, (H, W, 3))).clip(0, 255).astype(np.uint8)
    page = cv2.cvtColor(sheet, cv2.COLOR_GRAY2BGR)
    src = np.float32([[0, 0], [page.shape[1], 0], [page.shape[1], page.shape[0]], [0, page.shape[0]]])
    M = cv2.getPerspectiveTransform(src, DST)
    warped = cv2.warpPerspective(page, M, (W, H))
    mask = cv2.warpPerspective(np.full(page.shape[:2], 255, np.uint8), M, (W, H))
    img = np.where(mask[..., None] > 0, warped, table)
    if shadow:
        yy, xx = np.mgrid[0:H, 0:W]
        shade = np.ones((H, W), np.float32)
        shade[(yy > 1300) & (xx > 1700)] = 0.55
        img = (img * cv2.GaussianBlur(shade, (0, 0), 90)[..., None]).clip(0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (5, 5), 0)
    img = (img + rng.normal(0, 4, img.shape)).clip(0, 255).astype(np.uint8)
    for _ in range(rotate):
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    return img


@pytest.fixture(scope="module")
def sheet():
    return filled_sheet()


def test_page_corners_found(sheet):
    gray = cv2.cvtColor(photo_of(sheet), cv2.COLOR_BGR2GRAY)
    quad = docscan.find_page_quad(gray)
    assert quad is not None
    assert np.abs(quad - DST).max() < 40                     # within ~1% of the true corners


def test_full_page_scan_is_left_alone(sheet):
    assert docscan.find_page_quad(sheet) is None


def test_shadow_removed_and_paper_white(sheet):
    page = docscan.scan_document(photo_of(sheet), "gray")
    assert page.shape == (config.CANONICAL_HEIGHT, config.CANONICAL_WIDTH)
    lit, shaded = page[1500:1700, 100:300], page[1900:2100, 1250:1450]     # blank paper in light and in shadow
    assert lit.mean() > 235 and shaded.mean() > 235
    bw = docscan.scan_document(photo_of(sheet), "bw")
    assert set(np.unique(bw)) <= {0, 255}


@pytest.mark.parametrize("rotate", [0, 1, 2])
def test_bubbles_read_from_a_photo(tmp_path, sheet, rotate):
    path = tmp_path / "photo.jpg"
    cv2.imwrite(str(path), photo_of(sheet, rotate=rotate), [cv2.IMWRITE_JPEG_QUALITY, 88])
    res, _, _ = process_scan_full(str(path), config.NO_KEYS_PATH, config.LAYOUT_PATH, config.BASELINE_PATH)
    assert res["registration"] == "12345"
    assert all(res["answers"][str(q)] == "ABCD"[q % 4] for q in range(1, 11))

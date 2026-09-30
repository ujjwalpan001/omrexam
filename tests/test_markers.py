import cv2
import numpy as np
import pytest

from omr import config
from omr.exceptions import MarkerError
from omr.markers import detect_and_orient_markers, find_markers, load_layout

LAYOUT = load_layout(config.LAYOUT_PATH)
PX = config.CANONICAL_WIDTH / config.A4_WIDTH_MM


def mock_page(draw_frame=True, rotate=0, frame_scale=1.0):
    """White A4 with the thick frame and solid header bar drawn from layout.json."""
    img = np.full((config.CANONICAL_HEIGHT, config.CANONICAL_WIDTH), 255, np.uint8)
    fr = LAYOUT["frame"]
    if draw_frame:
        x0, y0 = int(fr["x0"] * PX), int(fr["y0"] * PX)
        x1, y1 = int((fr["x0"] + (fr["x1"] - fr["x0"]) * frame_scale) * PX), int(fr["y1"] * PX)
        t = int(fr["stroke"] * PX)
        cv2.rectangle(img, (x0, y0), (x1, y1), 0, -1)
        cv2.rectangle(img, (x0 + t, y0 + int(fr["header_h"] * PX)), (x1 - t, y1 - t), 255, -1)
    for _ in range(rotate):
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    return img


def test_find_four_corners():
    corners = find_markers(mock_page(), LAYOUT)
    assert set(corners) == {"TL", "TR", "BR", "BL"}
    for name, c in LAYOUT["markers"].items():
        assert abs(corners[name][0] - c["cx"] * PX) < 3
        assert abs(corners[name][1] - c["cy"] * PX) < 3


def test_reject_missing_frame():
    with pytest.raises(MarkerError):
        find_markers(mock_page(draw_frame=False), LAYOUT)


def test_reject_wrong_shape():
    with pytest.raises(MarkerError):
        find_markers(mock_page(frame_scale=2.0), LAYOUT)


@pytest.mark.parametrize("rotate", [1, 2, 3])
def test_detect_and_orient(rotate):
    oriented, corners = detect_and_orient_markers(mock_page(rotate=rotate), LAYOUT)
    assert oriented.shape == (config.CANONICAL_HEIGHT, config.CANONICAL_WIDTH)
    tl = LAYOUT["markers"]["TL"]
    assert abs(corners["TL"][0] - tl["cx"] * PX) < 3
    assert abs(corners["TL"][1] - tl["cy"] * PX) < 3

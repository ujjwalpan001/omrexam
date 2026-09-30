import pytest
import numpy as np
import cv2
from omr.bubbles import measure_bubble_fill, binarize_for_bubbles
from omr import config

def test_measure_bubble_fill():
    # Canonical is A4_WIDTH_MM x A4_HEIGHT_MM at 200 DPI
    px_per_mm = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    
    cx_mm, cy_mm = 10.0, 10.0
    cx = int(cx_mm * px_per_mm)
    cy = int(cy_mm * px_per_mm)
    radius_mm = 2.2
    
    r = int(radius_mm * config.BUBBLE_SAMPLING_RADIUS_FACTOR * px_per_mm)
    
    # 1. Full fill
    img_full = np.zeros((200, 200), dtype=np.uint8)
    cv2.circle(img_full, (cx, cy), r + 2, 255, -1)
    
    fill = measure_bubble_fill(img_full, cx_mm, cy_mm, radius_mm)
    assert fill > 0.95
    
    # 2. Empty
    img_empty = np.zeros((200, 200), dtype=np.uint8)
    fill_empty = measure_bubble_fill(img_empty, cx_mm, cy_mm, radius_mm)
    assert fill_empty == 0.0
    
    # 3. Half fill
    img_half = np.zeros((200, 200), dtype=np.uint8)
    cv2.rectangle(img_half, (cx - r, cy - r), (cx, cy + r), 255, -1)
    fill_half = measure_bubble_fill(img_half, cx_mm, cy_mm, radius_mm)
    assert 0.4 < fill_half < 0.6

def test_binarize_for_bubbles():
    config.BINARIZE_METHOD = "otsu"
    img = np.ones((100, 100), dtype=np.uint8) * 200
    cv2.circle(img, (50, 50), 20, 50, -1) # Dark circle
    
    thresh = binarize_for_bubbles(img)
    # The dark circle should become white (255)
    assert thresh[50, 50] == 255
    # The light background should become black (0)
    assert thresh[10, 10] == 0
    config.BINARIZE_METHOD = "adaptive" # restore

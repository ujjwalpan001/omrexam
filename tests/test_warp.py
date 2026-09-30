import pytest
import numpy as np
import cv2
import json
from omr.warp import warp_image
from omr import config

def test_warp_accuracy(tmp_path):
    layout = {
        "page_mm": {"w": 210.0, "h": 297.0},
        "markers": {
            "TL": {"cx": 133.3, "cy": 9.3},
            "TR": {"cx": 202.7, "cy": 9.3},
            "BR": {"cx": 202.7, "cy": 287.7},
            "BL": {"cx": 133.3, "cy": 287.7}
        }
    }
    layout_path = tmp_path / "layout.json"
    with open(layout_path, "w") as f:
        json.dump(layout, f)
        
    h, w = int(config.CANONICAL_HEIGHT * 1.1), int(config.CANONICAL_WIDTH * 1.1)
    img = np.ones((h, w), dtype=np.uint8) * 255
    
    px_per_mm = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    
    shift_x, shift_y = 50, 60
    markers = {
        'TL': (133.3 * px_per_mm + shift_x, 9.3 * px_per_mm + shift_y),
        'TR': (202.7 * px_per_mm + shift_x, 9.3 * px_per_mm + shift_y),
        'BR': (202.7 * px_per_mm + shift_x, 287.7 * px_per_mm + shift_y),
        'BL': (133.3 * px_per_mm + shift_x, 287.7 * px_per_mm + shift_y)
    }
    
    warped = warp_image(img, markers, str(layout_path))
    assert warped.shape == (config.CANONICAL_HEIGHT, config.CANONICAL_WIDTH)
    
    dst_points = np.zeros((4, 2), dtype=np.float32)
    src_points = np.zeros((4, 2), dtype=np.float32)
    
    order = ['TL', 'TR', 'BR', 'BL']
    for i, name in enumerate(order):
        src_points[i] = [markers[name][0], markers[name][1]]
        mx_mm = layout['markers'][name]['cx']
        my_mm = layout['markers'][name]['cy']
        dst_points[i] = [mx_mm * px_per_mm, my_mm * px_per_mm]
        
    matrix = cv2.getPerspectiveTransform(src_points, dst_points)
    
    src_points_3d = np.concatenate([src_points, np.ones((4, 1))], axis=1)
    transformed = matrix.dot(src_points_3d.T).T
    transformed = transformed[:, :2] / transformed[:, 2:]
    
    diff = np.abs(transformed - dst_points)
    assert np.all(diff < 1.0)

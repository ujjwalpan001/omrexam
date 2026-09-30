import cv2
import numpy as np
import json
from typing import Dict, Tuple
from . import config

def warp_image(image: np.ndarray, markers: Dict[str, Tuple[float, float]], layout) -> np.ndarray:
    """Warp to the canonical A4 size; `layout` is a layout dict or a path to layout.json."""
    if isinstance(layout, str):
        with open(layout, 'r') as f:
            layout = json.load(f)
        
    px_per_mm = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    
    dst_points = np.zeros((4, 2), dtype=np.float32)
    src_points = np.zeros((4, 2), dtype=np.float32)
    
    order = ['TL', 'TR', 'BR', 'BL']
    for i, name in enumerate(order):
        src_points[i] = [markers[name][0], markers[name][1]]
        mx_mm = layout['markers'][name]['cx']
        my_mm = layout['markers'][name]['cy']
        dst_points[i] = [mx_mm * px_per_mm, my_mm * px_per_mm]
        
    matrix = cv2.getPerspectiveTransform(src_points, dst_points)
    warped = cv2.warpPerspective(
        image, 
        matrix, 
        (config.CANONICAL_WIDTH, config.CANONICAL_HEIGHT),
        flags=cv2.INTER_LINEAR
    )
    
    return warped

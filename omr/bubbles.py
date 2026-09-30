import cv2
import numpy as np
from typing import Dict, Tuple, List
import json
import os
from . import config

def binarize_for_bubbles(warped_img: np.ndarray) -> np.ndarray:
    blurred = cv2.GaussianBlur(warped_img, (3, 3), 0)
    if config.BINARIZE_METHOD == "adaptive":
        thresh = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, config.ADAPTIVE_BLOCK_SIZE, config.ADAPTIVE_C
        )
    else:
        _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return thresh

def measure_bubble_fill(thresh_img: np.ndarray, cx_mm: float, cy_mm: float, radius_mm: float) -> float:
    px_per_mm = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    cx = int(cx_mm * px_per_mm)
    cy = int(cy_mm * px_per_mm)
    r = int(radius_mm * config.BUBBLE_SAMPLING_RADIUS_FACTOR * px_per_mm)
    
    h, w = thresh_img.shape
    if cx < 0 or cx >= w or cy < 0 or cy >= h:
        return 0.0
        
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (cx, cy), r, 255, -1)
    
    circle_area = np.pi * (r ** 2)
    if circle_area == 0:
        return 0.0
        
    masked = cv2.bitwise_and(thresh_img, mask)
    nonzero = cv2.countNonZero(masked)
    return float(nonzero) / circle_area

def process_sheet_bubbles(warped_img: np.ndarray, layout_path: str, baseline_path: str = None) -> Dict:
    with open(layout_path, 'r') as f:
        layout = json.load(f)
        
    baselines = {}
    if baseline_path and os.path.exists(baseline_path):
        with open(baseline_path, 'r') as f:
            baselines = json.load(f)
            
    thresh = binarize_for_bubbles(warped_img)
    bubble_r = layout['bubble_radius_mm']
    
    result_fills = {
        'registration': [],
        'paper_id': [],
        'questions': {}
    }
    
    for c_idx, col in enumerate(layout.get('registration', {}).get('columns', [])):
        col_fills = []
        for d, bubble in enumerate(col):
            fill = measure_bubble_fill(thresh, bubble['cx'], bubble['cy'], bubble_r)
            base = baselines.get('registration', {}).get(str(c_idx), {}).get(str(d), 0.0)
            col_fills.append(max(0.0, fill - base))
        result_fills['registration'].append(col_fills)
        
    for c_idx, col in enumerate(layout['paper_id']['columns']):
        col_fills = []
        for d, bubble in enumerate(col):
            fill = measure_bubble_fill(thresh, bubble['cx'], bubble['cy'], bubble_r)
            base = baselines.get('paper_id', {}).get(str(c_idx), {}).get(str(d), 0.0)
            col_fills.append(max(0.0, fill - base))
        result_fills['paper_id'].append(col_fills)
        
    for q in layout['questions']:
        q_num = str(q['q'])
        q_fills = {}
        for opt, bubble in q['options'].items():
            q_fills[opt] = measure_bubble_fill(thresh, bubble['cx'], bubble['cy'], bubble_r)
        result_fills['questions'][q_num] = q_fills
        
    return result_fills

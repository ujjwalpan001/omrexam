import json
import os
import cv2
import numpy as np
from typing import Dict, Tuple

from . import bubbles, config, docscan, grader, markers, preprocess, warp

def generate_debug_image(warped_img: np.ndarray, result_fills: Dict, layout_path: str, output_path: str, graded: Dict):
    debug_img = cv2.cvtColor(warped_img, cv2.COLOR_GRAY2BGR)
    
    with open(layout_path, 'r') as f:
        layout = json.load(f)
        
    px_per_mm = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    bubble_r = layout['bubble_radius_mm'] * px_per_mm
    
    corners = np.array([[m['cx'] * px_per_mm, m['cy'] * px_per_mm] for m in layout['markers'].values()], dtype=np.int32)
    cv2.polylines(debug_img, [corners], True, (255, 0, 255), 3)  # frame outline, TL-TR-BR-BL order

    def draw_bubble(cx_mm, cy_mm, status):
        cx = int(cx_mm * px_per_mm)
        cy = int(cy_mm * px_per_mm)
        color = (180, 180, 180)
        thickness = 1
        
        if status == "MARKED":
            color = (0, 255, 0)
            thickness = 2
        elif status in ("MULTIPLE", "UNCERTAIN"):
            color = (0, 0, 255)
            thickness = 2
            
        cv2.circle(debug_img, (cx, cy), int(bubble_r), color, thickness)

    for key in [k for k in ('registration', 'paper_id') if k in layout]:
        id_str = graded[key]
        for c, col in enumerate(layout[key]['columns']):
            decided = None
            if id_str != "INVALID" and c < len(id_str):
                decided = id_str[c]
                
            col_fills = result_fills[key][c]
            best_idx = 0
            best_val = -1
            for i, v in enumerate(col_fills):
                if v > best_val:
                    best_val = v
                    best_idx = i
            
            for d, bubble in enumerate(col):
                status = "NORMAL"
                if id_str != "INVALID" and str(d) == decided:
                    status = "MARKED"
                elif id_str == "INVALID" and best_idx == d:
                    status = "UNCERTAIN"
                    
                draw_bubble(bubble['cx'], bubble['cy'], status)
                
    for q in layout['questions']:
        q_num = str(q['q'])
        q_ans = graded['answers'].get(q_num, "BLANK")
        q_fills = result_fills['questions'].get(q_num, {})
        
        for opt, bubble in q['options'].items():
            status = "NORMAL"
            if q_ans == opt:
                status = "MARKED"
            elif q_ans in ("MULTIPLE", "UNCERTAIN"):
                if q_fills.get(opt, 0) >= config.UNCERTAIN_FILL:
                    status = "UNCERTAIN"
                    
            draw_bubble(bubble['cx'], bubble['cy'], status)
            
        text = " ".join([f"{opt}:{q_fills.get(opt,0):.2f}" for opt in "ABCD"])
        cy = int(q['options']["A"]['cy'] * px_per_mm)
        cx = int(layout['frame']['x0'] * px_per_mm) - 190  # label sits left of the frame
        cv2.putText(debug_img, text, (cx, cy + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)

    cv2.imwrite(output_path, debug_img)

PHONE_PAGE_PREFIX = "phonepage_"           # uploads the phone already cropped to the page (see the camera in web/index.html)


def is_phone_page(name: str) -> bool:
    return os.path.basename(name or "").startswith(PHONE_PAGE_PREFIX)


def locate_page(img, layout_path: str, phone_page: bool = False):
    """Find the answer-sheet frame. A flatbed scan works as is; a phone photo is first cropped, straightened and
    shadow-corrected (CamScanner style) when the direct attempt fails. `phone_page`: the phone already cropped and
    straightened the page, so only the light is evened out (the normal search remains the fallback)."""
    layout = markers.load_layout(layout_path)
    if phone_page:
        try:
            return markers.detect_and_orient_markers(docscan.remove_shadows(docscan.straighten(img, None)), layout)
        except markers.MarkerError:
            pass
    try:
        return markers.detect_and_orient_markers(img, layout)
    except markers.MarkerError as direct_error:
        try:
            return markers.detect_and_orient_markers(docscan.prepare_photo(img), layout)
        except markers.MarkerError:
            pass
        try:                                    # busy background: find the black frame itself, then the page around it
            page = docscan.prepare_photo(img, markers.page_quad_from_frame(img, layout))
            return markers.detect_and_orient_markers(page, layout)
        except markers.MarkerError:
            raise direct_error


def process_scan_full(filepath: str, keys_path: str, layout_path: str, baseline_path: str = None, located=None,
                      phone_page: bool = False):
    """Grade a scan; also return the straightened page image and the raw fill ratios.
    `located` = (oriented image, frame corners) when the page was already found (saves finding it twice)."""
    if located is None:
        located = locate_page(preprocess.load_image(filepath), layout_path, phone_page)
    oriented_img, marker_centers = located
    warped_img = warp.warp_image(oriented_img, marker_centers, layout_path)
    result_fills = bubbles.process_sheet_bubbles(warped_img, layout_path, baseline_path)
    with open(keys_path, 'r') as f:
        keys = json.load(f)
    graded = grader.grade_sheet(result_fills, keys)
    graded['file'] = os.path.basename(filepath)
    return graded, warped_img, result_fills


def process_scan(filepath: str, keys_path: str, layout_path: str, baseline_path: str = None, debug: bool = False, output_dir: str = None) -> Dict:
    img = preprocess.load_image(filepath)
    oriented_img, marker_centers = markers.detect_and_orient_markers(img, markers.load_layout(layout_path))
    warped_img = warp.warp_image(oriented_img, marker_centers, layout_path)
    result_fills = bubbles.process_sheet_bubbles(warped_img, layout_path, baseline_path)
    
    with open(keys_path, 'r') as f:
        keys = json.load(f)
        
    graded = grader.grade_sheet(result_fills, keys)
    graded['file'] = os.path.basename(filepath)
    
    if debug and output_dir:
        os.makedirs(output_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(filepath))[0]
        debug_path = os.path.join(output_dir, f"{base}_debug.jpg")
        generate_debug_image(warped_img, result_fills, layout_path, debug_path, graded)
        
    return graded


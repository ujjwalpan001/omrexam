import cv2
import numpy as np
import json
import os
from omr import preprocess, config

def generate_synthetic_data(pdf_path, layout_path, output_dir):
    img = preprocess.load_image(pdf_path)
    os.makedirs(output_dir, exist_ok=True)
    
    filled = img.copy()
    with open(layout_path, 'r') as f:
        layout = json.load(f)
        
    px_per_mm = config.CANONICAL_WIDTH / config.A4_WIDTH_MM
    bubble_r = layout['bubble_radius_mm'] * px_per_mm
    
    def fill_bubble(img_ref, cx_mm, cy_mm, intensity=50, radius_ratio=0.8):
        cx = int(cx_mm * px_per_mm)
        cy = int(cy_mm * px_per_mm)
        r = int(bubble_r * radius_ratio)
        cv2.circle(img_ref, (cx, cy), r, intensity, -1)
        
    reg_val = "10203"
    for c, col in enumerate(layout['registration']['columns']):
        d = int(reg_val[c])
        fill_bubble(filled, col[d]['cx'], col[d]['cy'])
        
    pid_val = "100123"
    for c, col in enumerate(layout['paper_id']['columns']):
        d = int(pid_val[c])
        fill_bubble(filled, col[d]['cx'], col[d]['cy'])
        
    expected_ans = {}
    for q in layout['questions']:
        q_n = str(q['q'])
        if q_n == "1":
            fill_bubble(filled, q['options']['A']['cx'], q['options']['A']['cy'])
            expected_ans[q_n] = "A"
        elif q_n == "2":
            fill_bubble(filled, q['options']['B']['cx'], q['options']['B']['cy'])
            expected_ans[q_n] = "B"
        elif q_n == "3":
            fill_bubble(filled, q['options']['C']['cx'], q['options']['C']['cy'])
            expected_ans[q_n] = "C"
        elif q_n == "4":
            fill_bubble(filled, q['options']['D']['cx'], q['options']['D']['cy'])
            expected_ans[q_n] = "D"
        elif q_n == "5":
            expected_ans[q_n] = "BLANK"
        elif q_n == "6":
            fill_bubble(filled, q['options']['A']['cx'], q['options']['A']['cy'])
            fill_bubble(filled, q['options']['B']['cx'], q['options']['B']['cy'])
            expected_ans[q_n] = "MULTIPLE"
        elif q_n == "7":
            fill_bubble(filled, q['options']['A']['cx'], q['options']['A']['cy'], intensity=170, radius_ratio=0.4)
            expected_ans[q_n] = "UNCERTAIN"
        else:
            fill_bubble(filled, q['options']['A']['cx'], q['options']['A']['cy'])
            expected_ans[q_n] = "A"
            
    cv2.imwrite(os.path.join(output_dir, "clean.png"), filled)
    
    h, w = filled.shape
    center = (w // 2, h // 2)
    shift_px = 3.0 * px_per_mm

    def affine(angle=0.0, scale=1.0, dx=0.0, dy=0.0):
        M = cv2.getRotationMatrix2D(center, angle, scale)
        M[0, 2] += dx
        M[1, 2] += dy
        return cv2.warpAffine(filled, M, (w, h), borderValue=255)

    variants = {
        "clean": filled,
        "rotated_1_5": affine(angle=1.5),
        "rotated_neg_1_5": affine(angle=-1.5),
        "shift_3mm": affine(dx=shift_px, dy=shift_px),
        "scaled_98": affine(scale=0.98),
        "scaled_102": affine(scale=1.02),
        "combined": affine(angle=-1.2, scale=1.02, dx=shift_px, dy=-shift_px),
        "rotated_180": cv2.rotate(filled, cv2.ROTATE_180),
        "rotated_90": cv2.rotate(filled, cv2.ROTATE_90_CLOCKWISE),
        "rotated_270": cv2.rotate(filled, cv2.ROTATE_90_COUNTERCLOCKWISE),
    }

    rng = np.random.default_rng(0)
    gradient = np.tile(np.linspace(0, 50, w, dtype=np.uint8), (h, 1))
    dark = cv2.subtract(filled, gradient)
    dark = cv2.GaussianBlur(dark, (3, 3), 0)
    noise = rng.normal(0, 10, (h, w)).astype(np.int16)
    variants["noise_lighting"] = np.clip(dark.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    for name, im in variants.items():
        cv2.imwrite(os.path.join(output_dir, name + ".png"), im)

    return expected_ans

if __name__ == "__main__":
    generate_synthetic_data(config.SHEET_PDF_PATH, config.LAYOUT_PATH, os.path.join(config.OUTPUT_DIR, "synthetic"))

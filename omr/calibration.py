"""Per-bubble baseline fill of a blank printed sheet (removes the printed digits' ink from ID bubbles)."""
from typing import Dict

from . import bubbles, markers, preprocess, warp


def compute_baseline(image_path: str, layout_path: str) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Measure every ID bubble on a blank sheet (image or PDF) and return {section: {col: {digit: fill}}}."""
    img = preprocess.load_image(image_path)
    layout = markers.load_layout(layout_path)
    oriented, corners = markers.detect_and_orient_markers(img, layout)
    warped = warp.warp_image(oriented, corners, layout)
    fills = bubbles.process_sheet_bubbles(warped, layout_path, None)
    return {
        key: {str(c): {str(d): f for d, f in enumerate(col)} for c, col in enumerate(fills[key])}
        for key in ("registration", "paper_id") if fills.get(key)
    }

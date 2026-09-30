import os

# Project folders
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
LAYOUT_PATH = os.path.join(DATA_DIR, "layout.json")
KEYS_PATH = os.path.join(DATA_DIR, "keys.json")
NO_KEYS_PATH = os.path.join(DATA_DIR, "keys_none.json")
BASELINE_PATH = os.path.join(DATA_DIR, "baseline.json")
SHEET_PDF_PATH = os.path.join(ROOT_DIR, "sheet", "omr_sheet.pdf")

DPI = 200
MM_TO_INCH = 25.4
A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0

CANONICAL_WIDTH = int(A4_WIDTH_MM * DPI / MM_TO_INCH)
CANONICAL_HEIGHT = int(A4_HEIGHT_MM * DPI / MM_TO_INCH)

# Alignment frame (thick black outline of the answer strip, header bar on top)
FRAME_AREA_MIN_RATIO = 0.6     # contour area vs expected frame area
FRAME_AREA_MAX_RATIO = 1.5
FRAME_ASPECT_TOLERANCE = 0.15  # relative error of long/short side ratio
FRAME_RECT_FILL_MIN = 0.95     # contour area / min-area-rectangle area
HEADER_INK_MIN = 0.8           # fraction of the header bar that must be dark (orientation check)

# Bubble measurement
BUBBLE_SAMPLING_RADIUS_FACTOR = 0.75
BINARIZE_METHOD = "adaptive"  # "adaptive" or "otsu"
ADAPTIVE_BLOCK_SIZE = 35
ADAPTIVE_C = 10

# Decision thresholds
MIN_FILL = 0.35
MIN_GAP = 0.15
UNCERTAIN_FILL = 0.2

# Scoring
SCORE_CORRECT = 1.0
SCORE_INCORRECT = 0.0

"""Shared fixtures: builds the synthetic scan set once per test session."""
import json
import os
import subprocess
import sys

import pytest

from omr import config
from make_test_data import generate_synthetic_data


@pytest.fixture(scope="session")
def setup_synthetic():
    out = os.path.join(config.OUTPUT_DIR, "synthetic")
    os.makedirs(out, exist_ok=True)
    baseline = os.path.join(out, "baseline.json")  # never overwrite data/baseline.json
    subprocess.run([sys.executable, "calibrate.py", config.SHEET_PDF_PATH, "--output", baseline], check=True)
    expected = generate_synthetic_data(config.SHEET_PDF_PATH, config.LAYOUT_PATH, out)
    
    keys = {"100123": ["A"] * 25}
    keys_path = os.path.join(out, "keys.json")
    with open(keys_path, "w") as f:
        json.dump(keys, f)
        
    return out, baseline, expected, keys_path

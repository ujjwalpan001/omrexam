import os

import pytest

from omr import config
from omr.pipeline import process_scan


def run_pipeline_on(image_name, tdir, baseline, keys):
    return process_scan(os.path.join(tdir, image_name), keys, config.LAYOUT_PATH, baseline)

def assert_expected(res, exp):
    assert res['registration'] == "10203"
    assert res['paper_id'] == "100123"
    for k, v in exp.items():
        assert res['answers'][k] == v

VARIANTS = [
    "clean", "rotated_1_5", "rotated_neg_1_5", "shift_3mm", "scaled_98",
    "scaled_102", "combined", "rotated_180", "rotated_90", "rotated_270",
    "noise_lighting",
]


@pytest.mark.parametrize("name", VARIANTS)
def test_variant(setup_synthetic, name):
    tdir, baseline, exp, keys = setup_synthetic
    res = run_pipeline_on(name + ".png", tdir, baseline, keys)
    assert_expected(res, exp)


def test_flags_and_review(setup_synthetic):
    tdir, baseline, exp, keys = setup_synthetic
    res = run_pipeline_on("clean.png", tdir, baseline, keys)
    assert res['needs_review'] is True
    assert "Q6 multiple" in res['flags']
    assert "Q7 uncertain" in res['flags']
    assert res['answers']['5'] == "BLANK"

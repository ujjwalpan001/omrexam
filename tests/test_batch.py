import csv
import shutil
import subprocess
import sys

from omr import config

OUT = config.OUTPUT_DIR + "/synthetic"


def test_batch_runs(tmp_path, setup_synthetic):
    scans = tmp_path / "scans"
    scans.mkdir()
    shutil.copy(OUT + "/clean.png", scans / "a.png")
    shutil.copy(OUT + "/rotated_180.png", scans / "b.png")
    (scans / "bad.png").write_bytes(b"not an image")
    out = tmp_path / "out"
    subprocess.run([sys.executable, "batch.py", str(scans), "--key", OUT + "/keys.json",
                    "--out", str(out)], check=True)
    rows = list(csv.DictReader(open(out / "results.csv")))
    assert [r["file"] for r in rows] == ["a.png", "b.png", "bad.png"]
    assert rows[0]["registration"] == "10203" and rows[1]["paper_id"] == "100123"
    assert rows[2]["needs_review"] == "True" and "ERROR" in rows[2]["flags"]
    assert (out / "a_debug.jpg").exists()

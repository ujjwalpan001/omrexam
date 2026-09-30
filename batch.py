"""Batch CLI: grade every scan in a folder and write results.csv plus JSON/debug output."""
import argparse
import csv
import json
import os
import sys
from typing import Dict, List

from omr import config
from omr.pipeline import process_scan

EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".pdf")


def list_scans(folder: str) -> List[str]:
    """Return sorted scan file paths in a folder."""
    return sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(EXTENSIONS)
    )


def error_row(path: str, message: str) -> Dict:
    """Result record for a sheet that could not be processed."""
    return {
        "file": os.path.basename(path), "registration": "", "paper_id": "",
        "answers": {}, "score": "", "total": "", "needs_review": True,
        "flags": [f"ERROR: {message}"],
    }


def write_csv(results: List[Dict], path: str, n_questions: int) -> None:
    """Write one CSV row per sheet, with a column per question."""
    header = ["file", "registration", "paper_id", "score", "total", "needs_review", "flags"]
    header += [f"Q{i}" for i in range(1, n_questions + 1)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in results:
            row = [r["file"], r["registration"], r["paper_id"], r["score"], r["total"],
                   r["needs_review"], "; ".join(r["flags"])]
            row += [r["answers"].get(str(i), "") for i in range(1, n_questions + 1)]
            w.writerow(row)


def main() -> None:
    p = argparse.ArgumentParser(description="Grade all scans in a folder.")
    p.add_argument("folder", nargs="?", default="scans")
    p.add_argument("--key", default=config.KEYS_PATH)
    p.add_argument("--layout", default=config.LAYOUT_PATH)
    p.add_argument("--baseline", default=config.BASELINE_PATH)
    p.add_argument("--out", default=os.path.join(config.OUTPUT_DIR, "results"))
    p.add_argument("--no-debug", action="store_true", help="skip debug images")
    args = p.parse_args()

    scans = list_scans(args.folder)
    if not scans:
        print(f"No scans found in {args.folder}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(args.out, exist_ok=True)

    results = []
    for path in scans:
        try:
            r = process_scan(path, args.key, args.layout, args.baseline,
                             debug=not args.no_debug, output_dir=args.out)
        except Exception as e:  # one bad sheet must not stop the batch
            r = error_row(path, str(e))
        results.append(r)
        base = os.path.splitext(r["file"])[0]
        with open(os.path.join(args.out, base + ".json"), "w") as f:
            json.dump(r, f, indent=2)
        print(f"{r['file']}: score={r['score']} review={r['needs_review']}")

    with open(args.layout) as f:
        n_q = len(json.load(f)["questions"])
    write_csv(results, os.path.join(args.out, "results.csv"), n_q)
    print(f"Wrote {os.path.join(args.out, 'results.csv')} ({len(results)} sheets)")


if __name__ == "__main__":
    main()

import argparse
import json

from omr import config
from omr.calibration import compute_baseline


def main():
    parser = argparse.ArgumentParser(description="Store the blank-sheet baseline for the ID bubbles.")
    parser.add_argument("scan_path", help="scan (or PDF) of ONE blank printed sheet")
    parser.add_argument("--layout", default=config.LAYOUT_PATH)
    parser.add_argument("--output", default=config.BASELINE_PATH)
    args = parser.parse_args()

    with open(args.output, "w") as f:
        json.dump(compute_baseline(args.scan_path, args.layout), f, indent=2)
    print(f"Saved baseline to {args.output}")


if __name__ == "__main__":
    main()

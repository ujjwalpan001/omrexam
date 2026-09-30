import argparse
import json
import sys
from omr import config
from omr.pipeline import process_scan
from omr.exceptions import MarkerError

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scan_path")
    parser.add_argument("--key", default=None, help="optional; without it only answers are printed")
    parser.add_argument("--answers", action="store_true", help="print just '1-A' style lines")
    parser.add_argument("--layout", default=config.LAYOUT_PATH)
    parser.add_argument("--baseline", default=config.BASELINE_PATH)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    
    try:
        result = process_scan(
            filepath=args.scan_path,
            keys_path=args.key or config.NO_KEYS_PATH,
            layout_path=args.layout,
            baseline_path=args.baseline,
            debug=args.debug,
            output_dir=config.OUTPUT_DIR + "/debug" if args.debug else None
        )
        if args.answers or not args.key:
            print(f"Registration: {result['registration']}  Paper ID: {result['paper_id']}")
            for q, a in sorted(result["answers"].items(), key=lambda kv: int(kv[0])):
                print(f"{q}-{a}")
            if result["flags"]:
                print("Flags:", "; ".join(result["flags"]))
        else:
            print(json.dumps(result, indent=2))
    except MarkerError as e:
        print(f"Error processing {args.scan_path}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

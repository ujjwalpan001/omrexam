"""Check that the fast shadow removal (now always used) reads your own photos exactly like the old full-size method.

    python compare_shadows.py photos/                 every .jpg/.jpeg/.png in the folder
    python compare_shadows.py photos/ --layout output/formats/10/layout.json

Each photo is read twice - normal and fast - and the registration number, paper ID and every answer are compared.
Answers only need the sheet format (layout), not an exam, so any filled sheet of that format works. Uses the 10-question
format unless --layout is given; for other formats use output/formats/<count>/layout.json (made by the server)."""
import argparse
import glob
import os
import time

import cv2

from omr import config, docscan, preprocess
from omr.bubbles import process_sheet_bubbles
from omr.grader import decide_question, decode_id_column
from omr.pipeline import locate_page
from omr.warp import warp_image


def read(path: str, layout: str, baseline: str, fast: bool) -> dict:
    docscan.FAST_SHADOWS = fast
    start = time.perf_counter()
    oriented, corners = locate_page(preprocess.load_image(path), layout)
    fills = process_sheet_bubbles(warp_image(oriented, corners, layout), layout, baseline)
    return {"seconds": time.perf_counter() - start,
            "registration": "".join(decode_id_column(c) for c in fills["registration"]),
            "paper_id": "".join(decode_id_column(c) for c in fills["paper_id"]),
            "answers": {q: decide_question(f) for q, f in fills["questions"].items()},
            "fills": fills["questions"]}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--layout", default=os.path.join(config.OUTPUT_DIR, "formats", "10", "layout.json"))
    a = p.parse_args()
    layout = a.layout if os.path.exists(a.layout) else config.LAYOUT_PATH
    baseline = os.path.join(os.path.dirname(layout), "baseline.json")
    baseline = baseline if os.path.exists(baseline) else config.BASELINE_PATH
    cv2.setNumThreads(1)
    photos = sorted(f for ext in ("jpg", "jpeg", "png", "JPG", "JPEG", "PNG") for f in glob.glob(os.path.join(a.folder, "*." + ext)))
    if not photos:
        raise SystemExit(f"No photos in {a.folder}")
    same = differ = failed = 0
    t_normal = t_fast = 0.0
    for path in photos:
        name = os.path.basename(path)
        try:
            old, new = read(path, layout, baseline, False), read(path, layout, baseline, True)
        except Exception as e:
            print(f"  {name}: could not read the sheet ({e})")
            failed += 1
            continue
        t_normal, t_fast = t_normal + old["seconds"], t_fast + new["seconds"]
        changes = [f"registration {old['registration']} -> {new['registration']}"] if old["registration"] != new["registration"] else []
        if old["paper_id"] != new["paper_id"]:
            changes.append(f"paper ID {old['paper_id']} -> {new['paper_id']}")
        for q, ans in old["answers"].items():
            if new["answers"][q] != ans:
                fo = ", ".join(f"{k}={v:.2f}" for k, v in old["fills"][q].items())
                fn = ", ".join(f"{k}={v:.2f}" for k, v in new["fills"][q].items())
                changes.append(f"Q{q}: {ans} -> {new['answers'][q]}  (normal {fo} | fast {fn})")
        drift = max(abs(new["fills"][q][k] - v) for q, f in old["fills"].items() for k, v in f.items())
        if changes:
            differ += 1
            print(f"  {name}: DIFFERENT")
            for c in changes:
                print(f"      {c}")
        else:
            same += 1
            print(f"  {name}: same  (largest bubble change {drift:.3f}; {old['seconds']:.2f}s -> {new['seconds']:.2f}s)")
    print(f"\n{same} photo(s) identical, {differ} different, {failed} unreadable.")
    if same + differ:
        print(f"Time: normal {t_normal:.1f}s, fast {t_fast:.1f}s ({t_normal / max(t_fast, 1e-6):.1f}x faster)")
    if differ == 0 and same:
        print("All good: the fast method reads these photos exactly like the old one.")
    elif differ:
        print("Some photos differ - send this output so the bubbles above can be looked at.")


if __name__ == "__main__":
    main()

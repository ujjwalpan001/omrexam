#!/usr/bin/env python3
"""
Single source of truth for the OMR sheet.

Set frame.y1 in sheet_config.json to stretch the strip to the page bottom (row spacing grows automatically).
Reads  sheet/sheet_config.json (OMR strip) and sheet/exam.json (header details + question text) and renders
    sheet/omr_sheet.tex  (from templates/omr_sheet.tex.j2 with Jinja2; compile with pdflatex, print at 100%)
    data/layout.json     (all coordinates in mm, origin = top-left of A4, y down)

Change the sheet by editing sheet_config.json (question count, options, ID digits, titles, header text,
section order) and re-running this script. All numbers are millimetres.
"""
import json
import os
import sys

from jinja2 import Environment, FileSystemLoader

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
DEFAULT_CONFIG = os.path.join(HERE, "sheet_config.json")


class SheetError(ValueError):
    """The requested sheet cannot be built (too many questions, bad config)."""


def load_config(n_questions=None) -> dict:
    """Load sheet_config.json; optionally choose the number of questions (must be in allowed_counts)."""
    with open(DEFAULT_CONFIG) as f:
        cfg = json.load(f)
    if n_questions is not None:
        cfg["num_questions"] = n_questions
    if cfg["num_questions"] not in cfg["allowed_counts"]:
        raise SheetError(f"Number of questions must be one of {cfg['allowed_counts']}")
    return cfg


def section_specs(cfg: dict) -> list:
    """Sections stacked on the answer strip: registration number, questions, paper ID."""
    ids, n = cfg["id_sections"], cfg["num_questions"]
    return [dict(type="id", key="registration", **ids["registration"]),
            dict(type="questions", key="questions", start=1, count=n, first=1, last=n, **cfg["question_section"]),
            dict(type="id", key="paper_id", **ids["paper_id"])]


def r3(v: float) -> float:
    return round(v, 3)


def build_id_section(spec: dict, cfg: dict, y0: float, x0: float, x1: float) -> dict:
    """Digit-bubble columns (one per ID digit) plus handwriting boxes above each."""
    st, n = cfg["id_style"], spec["digits"]
    centre = (x0 + x1) / 2
    fit = (x1 - x0 - st["box_w"] - 6.0) / (n - 1) if n > 1 else st["col_pitch"]      # keep every column inside the box
    pitch = min(st["col_pitch"], fit)
    columns = []
    for i in range(n):
        x = r3(centre + (i - (n - 1) / 2) * pitch)
        by = y0 + st["box_dy"]
        columns.append(dict(
            x=x,
            box=dict(x0=r3(x - st["box_w"] / 2), y0=r3(by), x1=r3(x + st["box_w"] / 2), y1=r3(by + st["box_h"])),
            bubbles=[dict(digit=d, cx=x, cy=r3(y0 + st["first_row_dy"] + d * st["row_pitch"])) for d in range(10)],
        ))
    y1 = y0 + st["first_row_dy"] + 9 * st["row_pitch"] + st["bottom_pad"]
    return dict(type="id", key=spec["key"], title=spec["title"].format(n=n), x0=x0, x1=x1, y0=r3(y0), y1=r3(y1),
                title_y=r3(y0 + st["title_dy"]), columns=columns)


def build_question_section(spec: dict, cfg: dict, y0: float, x0: float, x1: float) -> dict:
    """One row per question, one bubble per option."""
    st, opts, count = cfg["question_style"], spec["options"], spec["count"]
    first_x = x0 + st["first_option_dx"]
    heads = [dict(letter=L, cx=r3(first_x + k * st["option_pitch"])) for k, L in enumerate(opts)]
    rows = []
    for i in range(count):
        cy = r3(y0 + st["first_row_dy"] + i * st["row_pitch"])
        rows.append(dict(q=spec["start"] + i, cy=cy,
                         options=[dict(letter=h["letter"], cx=h["cx"], cy=cy) for h in heads]))
    g = spec.get("group_size") or 0
    seps = [r3(y0 + st["first_row_dy"] + (i - 0.5) * st["row_pitch"]) for i in range(g, count, g)] if g else []
    y1 = y0 + st["first_row_dy"] + (count - 1) * st["row_pitch"] + st["bottom_pad"]
    return dict(type="questions", key=spec["key"], title=spec["title"].format(first=spec["first"], last=spec["last"], n=count), x0=x0, x1=x1,
                y0=r3(y0), y1=r3(y1), title_y=r3(y0 + st["title_dy"]), header_y=r3(y0 + st["header_dy"]),
                number_x=r3(x0 + st["number_right_dx"]), option_heads=heads, rows=rows, separators=seps)


def stack_sections(cfg: dict, specs: list, k: float) -> list:
    """Stack one page's sections inside the frame with every row pitch multiplied by k."""
    cfg = dict(cfg, id_style=dict(cfg["id_style"], row_pitch=cfg["id_style"]["row_pitch"] * k),
               question_style=dict(cfg["question_style"], row_pitch=cfg["question_style"]["row_pitch"] * k))
    fr = cfg["frame"]
    x0, x1 = fr["x0"] + fr["margin_x"], fr["x1"] - fr["margin_x"]
    y = fr["y0"] + fr["header_h"] + fr["section_gap"]
    sections = []
    for spec in specs:
        build = build_id_section if spec["type"] == "id" else build_question_section
        sec = build(spec, cfg, y, x0, x1)
        sections.append(sec)
        y = sec["y1"] + fr["section_gap"]
    return sections


def stretch_factor(cfg: dict, specs: list) -> float:
    """Row-pitch multiplier that makes the frame end at frame.y1 (bubbles spread out for accuracy)."""
    fr = cfg["frame"]
    if "y1" not in fr:
        return 1.0
    bottom = lambda k: stack_sections(cfg, specs, k)[-1]["y1"] + fr["bottom_pad"]
    k = (fr["y1"] - bottom(0.0)) / (bottom(1.0) - bottom(0.0))
    return max(1.0, min(k, fr.get("max_stretch", 2.5)))


def build_model(cfg: dict) -> dict:
    """Compute every position on the answer strip."""
    fr, specs = cfg["frame"], section_specs(cfg)
    sections = stack_sections(cfg, specs, stretch_factor(cfg, specs))
    frame = dict(fr, y1=r3(sections[-1]["y1"] + fr["bottom_pad"]))
    if frame["y1"] > cfg["page"]["h"] - 5:
        raise SheetError(f"The answer strip would end at {frame['y1']} mm and not fit on the page")
    return dict(sections=sections, frame=frame, paper_area=cfg.get("paper_area"), bubble_r=cfg["bubble_radius"])


def build_layout(cfg: dict, model: dict) -> dict:
    """layout.json: what the scanner needs (frame corners, bubble centres)."""
    fr, half = model["frame"], model["frame"]["stroke"] / 2
    ox0, oy0, ox1, oy1 = fr["x0"] - half, fr["y0"] - half, fr["x1"] + half, fr["y1"] + half   # OUTER edge
    by_key = {s["key"]: s for s in model["sections"]}

    def id_cols(key):
        return [[dict(digit=b["digit"], cx=b["cx"], cy=b["cy"]) for b in c["bubbles"]] for c in by_key[key]["columns"]]

    return dict(
        page_mm=cfg["page"], bubble_radius_mm=cfg["bubble_radius"],
        frame=dict(x0=r3(ox0), y0=r3(oy0), x1=r3(ox1), y1=r3(oy1), stroke=fr["stroke"], header_h=fr["header_h"]),
        markers={"TL": dict(cx=r3(ox0), cy=r3(oy0)), "TR": dict(cx=r3(ox1), cy=r3(oy0)),
                 "BR": dict(cx=r3(ox1), cy=r3(oy1)), "BL": dict(cx=r3(ox0), cy=r3(oy1))},
        registration=dict(columns=id_cols("registration")),
        paper_id=dict(columns=id_cols("paper_id")),
        questions=[dict(q=r["q"], cy=r["cy"], num_x=by_key["questions"]["number_x"],
                        options={o["letter"]: dict(cx=o["cx"], cy=o["cy"]) for o in r["options"]})
                   for r in by_key["questions"]["rows"]],
    )


TEX_ESCAPES = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
               "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex_escape(text: str) -> str:
    return "".join(TEX_ESCAPES.get(ch, ch) for ch in str(text))


def render_tex(cfg: dict, model: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(os.path.join(HERE, "templates")),
        block_start_string="(%", block_end_string="%)",
        variable_start_string="((", variable_end_string="))",
        comment_start_string="(#", comment_end_string="#)",
        trim_blocks=True, lstrip_blocks=True, autoescape=False,
    )
    env.filters["tex"] = tex_escape
    env.globals["P"] = lambda x, y: f"({x:.3f}mm,{-y:.3f}mm)"     # TikZ point, y down from page top
    return env.get_template("omr_sheet.tex.j2").render(**model)


def load_exam(cfg: dict, path: str) -> dict:
    """Exam text (header details + questions). Returns {} if there is no exam file."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        exam = json.load(f)
    if len(exam["questions"]) != cfg["num_questions"]:
        raise SheetError(f"exam.json has {len(exam['questions'])} questions but the sheet is set to "
                         f"{cfg['num_questions']} (sheet_config.json -> num_questions). Make them equal.")
    exam.setdefault("roll_no_digits", cfg["id_sections"]["registration"]["digits"])
    exam.setdefault("roll_no_prefix", "")
    return exam


def text_context(cfg: dict, exam: dict) -> dict:
    """Text margins for the left area come from paper_area. Questions are split like the reference template:
    the first `page1_questions` go beside the answer strip (page 1), the rest use the full width on page 2."""
    pa, pg = cfg["paper_area"], cfg["page"]
    ctx = dict(text_margin=dict(left=pa["x0"], right=pg["w"] - pa["x1"], top=pa["y0"], bottom=pg["h"] - pa["y1"]),
               exam=exam or None, questions_p1=[], questions_p2=[])
    if exam:
        qs = exam["questions"]
        k = exam.get("page1_questions", len(qs))
        ctx.update(questions_p1=qs[:k], questions_p2=qs[k:])
    return ctx


def main() -> None:
    cfg = load_config()
    exam = load_exam(cfg, os.path.join(HERE, "exam.json"))
    model = dict(build_model(cfg), **text_context(cfg, exam))
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "layout.json"), "w") as f:
        json.dump(build_layout(cfg, model), f, indent=1)
    with open(os.path.join(HERE, "omr_sheet.tex"), "w") as f:
        f.write(render_tex(cfg, model))
    print("wrote sheet/omr_sheet.tex and data/layout.json")


if __name__ == "__main__":
    try:
        main()
    except SheetError as e:
        sys.exit(str(e))

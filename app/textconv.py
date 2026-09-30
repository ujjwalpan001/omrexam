"""Plain-text question -> LaTeX.  Teachers type normal text; only $...$ is treated as math."""
import re
from typing import List, Optional, Tuple

LETTERS = "ABCD"
# an option marker: "A)", "A.", "(A)", "a)" at the start or after whitespace
MARKER = re.compile(r"(?<!\S)(\*?)\(?([A-Da-d])[\.\)]\s+")   # a leading * marks the correct option
UNSAFE_MATH = re.compile(r"\\(input|include|write|openin|openout|read|immediate|catcode|csname|expandafter|def|edef|gdef|"
                         r"xdef|let|newcommand|renewcommand|usepackage|documentclass|verbatiminput|url|href|special|jobname)\b")
ESCAPES = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
           "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "$": r"\$"}


def parse_block(block: str) -> Tuple[str, List[str], Optional[int]]:
    """Split 'question text + options A-D' into (question, [4 option texts], index marked with * or None).

    Options may be on separate lines or on one line: 'A) 1  B) 2  C) 3  D) 4'."""
    block = block.replace("\r\n", "\n").strip()
    marks = list(MARKER.finditer(block))
    for i, a in enumerate(marks):
        if a.group(2).upper() != "A":
            continue
        chain, want = [a], 1
        for m in marks[i + 1:]:
            if m.group(2).upper() == LETTERS[want]:
                chain.append(m)
                want += 1
                if want == 4:
                    break
        if len(chain) == 4:
            question = block[:chain[0].start()].strip()
            ends = [m.start() for m in chain[1:]] + [len(block)]
            options = [" ".join(block[m.end():e].split()) for m, e in zip(chain, ends)]
            if not question:
                raise ValueError("write the question before the options")
            if any(not o for o in options):
                raise ValueError("an option is empty")
            starred = [k for k, m in enumerate(chain) if m.group(1)]
            return question, options, (starred[0] if len(starred) == 1 else None)
    raise ValueError("could not find options A), B), C) and D) in this order after the question")


def _escape_text(text: str) -> str:
    """Escape LaTeX specials; a bare power like x^2 becomes math ($x^{2}$)."""
    out = []
    for i, piece in enumerate(re.split(r"(\w\^-?\w+)", text)):
        if i % 2:
            base, power = piece.split("^")
            out.append(f"${base}^{{{power}}}$")
        else:
            out.append("".join(ESCAPES.get(ch, ch) for ch in piece))
    return "".join(out)


def _escape_plain(text: str) -> str:
    """Escape everything except $...$ math segments, which stay as typed."""
    parts = re.split(r"(\$[^$]*\$)", text)
    return "".join(p if len(p) >= 2 and p.startswith("$") and p.endswith("$") else _escape_text(p) for p in parts)


def _quotes(text: str) -> str:
    """Straight double quotes -> LaTeX opening/closing quotes."""
    res, open_q = [], True
    for i, ch in enumerate(text):
        if ch == '"':
            prev = text[i - 1] if i else " "
            res.append("``" if (open_q or prev in " ([") else "''")
            open_q = not open_q
        else:
            res.append(ch)
    return "".join(res)


def to_latex(text: str) -> str:
    """Make typed text safe and nicely typeset LaTeX."""
    for m in re.finditer(r"\$[^$]*\$", text):
        if UNSAFE_MATH.search(m.group(0)):
            raise ValueError("that math uses a command that is not allowed")
    text = _quotes(text.replace("\r\n", "\n").strip())
    text = _escape_plain(text)
    return re.sub(r"\n{2,}", r"\\par ", text).replace("\n", r"\\ ")


ANSWER_LINE = re.compile(r"^[ \t]*[*_]*(?:answer|ans|correct(?:[ \t]+answer)?|key)[*_]*[ \t]*[:=\-][ \t]*[*_]*\(?([A-Da-d])(?![A-Za-z])[^\n]*$", re.I | re.M)
NUM_PREFIX = re.compile(r"^\s*(?:q(?:uestion)?[ \t]*)?\d+[ \t]*[\.\):][ \t]+", re.I)
CONTINUATION = re.compile(r"^\s*(?:\*?\(?[Aa][\.\)][ \t]|[*_]*(?:answer|ans|correct|key)[*_]*[ \t]*[:=\-])", re.I)


def split_blocks(text: str) -> List[str]:
    """Questions are separated by blank lines. A block that starts with option A) or an Answer: line
    belongs to the question above it (people often put a blank line before the options)."""
    blocks: List[str] = []
    for raw in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        raw = raw.strip()
        if not raw:
            continue
        if blocks and CONTINUATION.match(raw):
            blocks[-1] += "\n" + raw
        else:
            blocks.append(raw)
    return blocks


def parse_bulk(text: str) -> Tuple[List[dict], List[str]]:
    """Parse many questions pasted together -> ([{text, options, correct}], [error messages])."""
    questions, errors = [], []
    text = text.replace("**", "")                     # AI assistants like to bold things
    for n, block in enumerate(split_blocks(text), 1):
        try:
            answers = ANSWER_LINE.findall(block)
            block = ANSWER_LINE.sub("", NUM_PREFIX.sub("", block, count=1)).strip()
            question, options, starred = parse_block(block)
            if len(answers) > 1:
                raise ValueError("more than one Answer line")
            if answers:
                correct = LETTERS.index(answers[0].upper())
            elif starred is not None:
                correct = starred
            else:
                raise ValueError("no correct answer (add a line like 'Answer: B')")
            for part in [question, *options]:
                to_latex(part)
            questions.append(dict(text=question, options=options, correct=correct))
        except ValueError as e:
            errors.append(f'Question {n} ("{" ".join(block.split())[:40]}..."): {e}')
    return questions, errors

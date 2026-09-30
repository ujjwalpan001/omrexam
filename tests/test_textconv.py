"""The question parser must accept what AI assistants typically produce."""
from app.textconv import parse_bulk

GOOD = """**Answer** is not here, this is the question?
A) one
B) two
C) three
D) four
**Answer: B**

1. Second question with numbering
A. red
B. green
C. blue
D. yellow
Correct Answer: C) blue

What is $2^3$?
A) 6
B) 8
C) 9
D) 16
Answer: B
"""


def test_markdown_bold_numbering_and_text_after_the_letter():
    questions, errors = parse_bulk(GOOD)
    assert errors == []
    assert [q["correct"] for q in questions] == [1, 2, 1]
    assert questions[1]["text"] == "Second question with numbering" and questions[1]["options"] == ["red", "green", "blue", "yellow"]
    assert questions[2]["text"] == "What is $2^3$?"


def test_an_answer_that_is_a_sentence_is_not_mistaken_for_a_letter():
    _, errors = parse_bulk("Q?\nA) a\nB) b\nC) c\nD) d\nAnswer: Because it is")
    assert len(errors) == 1 and "no correct answer" in errors[0]


def test_the_copied_prompt_format_round_trips():
    sample = "What is the capital of France?\nA) Berlin\nB) Paris\nC) Rome\nD) Madrid\nAnswer: B\n\nWhat is $2^3$?\nA) 6\nB) 8\nC) 9\nD) 16\nAnswer: B"
    questions, errors = parse_bulk(sample)
    assert errors == [] and [q["correct"] for q in questions] == [1, 1]

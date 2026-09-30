import pytest
from omr.grader import decide_question, decode_id_column, grade_sheet
from omr import config

def test_decide_question():
    # ANSWER
    assert decide_question({"A": 0.9, "B": 0.1, "C": 0.05, "D": 0.0}) == "A"
    
    # BLANK
    assert decide_question({"A": 0.1, "B": 0.1, "C": 0.1, "D": 0.1}) == "BLANK"
    
    # UNCERTAIN
    assert decide_question({"A": 0.25, "B": 0.1, "C": 0.05, "D": 0.0}) == "UNCERTAIN"
    
    # MULTIPLE
    assert decide_question({"A": 0.8, "B": 0.75, "C": 0.0, "D": 0.0}) == "MULTIPLE"

def test_decode_id_column():
    assert decode_id_column([0.1, 0.9, 0.1, 0.05, 0.0]) == "1"
    
    # Not enough gap
    assert decode_id_column([0.1, 0.9, 0.85, 0.0, 0.0]) == "?"
    
    # Too faint
    assert decode_id_column([0.1, 0.15, 0.1, 0.1, 0.1]) == "?"

def test_grade_sheet():
    result_fills = {
        'registration': [
            [0.9, 0.1], [0.1, 0.9]
        ],
        'paper_id': [
            [0.8, 0.1], [0.1, 0.8]
        ],
        'questions': {
            "1": {"A": 0.9, "B": 0.1, "C": 0.0, "D": 0.0},
            "2": {"A": 0.1, "B": 0.9, "C": 0.0, "D": 0.0},
            "3": {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}, # BLANK
        }
    }
    
    keys = {
        "01": ["A", "A", "C"]
    }
    
    res = grade_sheet(result_fills, keys)
    assert res['registration'] == "01"
    assert res['paper_id'] == "01"
    assert res['score'] == 1.0 # Q1 correct
    assert res['total'] == 3
    assert not res['needs_review']
    
    # Test invalid ID
    result_fills['paper_id'][0] = [0.1, 0.1]
    res2 = grade_sheet(result_fills, keys)
    assert res2['paper_id'] == "INVALID"
    assert res2['needs_review']
    assert "Paper ID col 1 invalid" in res2['flags']

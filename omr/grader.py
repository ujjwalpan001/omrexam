from typing import Dict, List
from . import config

def decide_question(fills: Dict[str, float]) -> str:
    sorted_opts = sorted(fills.items(), key=lambda x: x[1], reverse=True)
    best_opt, best_fill = sorted_opts[0]
    second_fill = sorted_opts[1][1]
    
    if best_fill < config.UNCERTAIN_FILL:
        return "BLANK"
    if config.UNCERTAIN_FILL <= best_fill < config.MIN_FILL:
        return "UNCERTAIN"
        
    if second_fill >= config.MIN_FILL and (best_fill - second_fill) < config.MIN_GAP:
        return "MULTIPLE"
        
    return best_opt

def decode_id_column(col_fills: List[float]) -> str:
    best_idx = 0
    best_val = -1.0
    for i, v in enumerate(col_fills):
        if v > best_val:
            best_val = v
            best_idx = i
            
    second_val = max([v for i, v in enumerate(col_fills) if i != best_idx] + [-1.0])
    
    if (best_val - second_val) > config.MIN_GAP and best_val > config.UNCERTAIN_FILL:
        return str(best_idx)
    return "?"

def grade_sheet(result_fills: Dict, keys: Dict) -> Dict:
    flags = []
    needs_review = False
    
    reg_str = ""
    for c, col in enumerate(result_fills['registration']):
        d = decode_id_column(col)
        reg_str += d
        if d == "?":
            flags.append(f"Registration col {c+1} invalid")
            
    pid_str = ""
    for c, col in enumerate(result_fills['paper_id']):
        d = decode_id_column(col)
        pid_str += d
        if d == "?":
            flags.append(f"Paper ID col {c+1} invalid")
            
    if "?" in pid_str or "?" in reg_str:
        needs_review = True
        
    answers = {}
    for q_num, fills in result_fills['questions'].items():
        ans = decide_question(fills)
        answers[q_num] = ans
        if ans in ("MULTIPLE", "UNCERTAIN"):
            needs_review = True
            flags.append(f"Q{q_num} {ans.lower()}")
            
    score = 0
    total = len(answers)
    key = keys.get(pid_str)
    
    if key is None:
        if not "?" in pid_str:
            flags.append(f"Unknown paper ID: {pid_str}")
        needs_review = True
    else:
        for q_num, ans in answers.items():
            try:
                idx = int(q_num) - 1
                if 0 <= idx < len(key):
                    if ans == key[idx]:
                        score += config.SCORE_CORRECT
                    elif ans not in ("BLANK", "MULTIPLE", "UNCERTAIN"):
                        score += config.SCORE_INCORRECT
            except ValueError:
                pass
                
    return {
        "registration": reg_str if "?" not in reg_str else "INVALID",
        "paper_id": pid_str if "?" not in pid_str else "INVALID",
        "answers": answers,
        "score": score,
        "total": total,
        "needs_review": needs_review,
        "flags": flags
    }

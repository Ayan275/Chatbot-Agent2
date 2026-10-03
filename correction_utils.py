from __future__ import annotations

import json
import os
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

STOP_WORDS = {
    "what", "is", "a", "an", "the", "are", "of", "in", "to", "for", "with", "on", "at", "by",
    "from", "about", "how", "why", "where", "when", "who", "which", "do", "does", "did",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must", "have",
    "has", "had", "having", "be", "been", "being", "am", "was", "were", "it", "this",
    "that", "these", "those", "i", "you", "he", "she", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "their", "our", "its", "and", "or", "but", "so",
    "if", "then", "else", "not", "no", "yes", "please", "define", "explain",
    "kya", "hai", "hote", "hain", "ko", "se", "aur", "mein", "ka", "ki", "ke", "tha", "the",
    "thi", "ek", "jo", "par", "bhi", "hi", "haan", "nahin", "na", "kar", "karna", "krna",
    "hota", "hoti", "hote", "huye", "hue", "huaye", "kuch", "isey", "usey", "yeh", "woh",
    "ye", "wo", "hotein", "kiya", "gaya",
}


def extract_keywords(text: str) -> set[str]:
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    words = cleaned.lower().split()
    return {w for w in words if w not in STOP_WORDS and len(w) >= 2}


def keyword_match(curr_keywords: set[str], saved_keywords: set[str]) -> bool:
    for kw_curr in curr_keywords:
        for kw_saved in saved_keywords:
            if kw_curr == kw_saved:
                return True
            if len(kw_curr) >= 3 and len(kw_saved) >= 3 and (kw_curr in kw_saved or kw_saved in kw_curr):
                return True
    return False


def compute_match_score(user_query: str, known_query: str) -> float:
    stripped = user_query.strip()
    known = known_query.strip()
    if not stripped or not known:
        return 0.0

    curr_keywords = extract_keywords(stripped)
    known_keywords = extract_keywords(known)
    if curr_keywords and known_keywords and keyword_match(curr_keywords, known_keywords):
        return 1.0

    char_ratio = SequenceMatcher(None, stripped.lower(), known.lower()).ratio()
    if char_ratio >= 0.86:
        return char_ratio

    if not curr_keywords or not known_keywords:
        return 0.0

    overlap = len(curr_keywords & known_keywords) / max(1, min(len(curr_keywords), len(known_keywords)))
    if overlap >= 0.5 and char_ratio >= 0.62:
        return max(char_ratio, overlap)
    return 0.0


def build_negative_constraint(blocked_response: str) -> str:
    return (
        "CRITICAL ATTENTION: The user previously rejected this specific structure/answer for this topic: "
        f"'{blocked_response}'. You are STRICTLY FORBIDDEN from repeating this fact order, language "
        "style, layout, or historical dates. You MUST deliver a completely refreshed alternative perspective "
        "or a practical real-world application breakout instead. Do not repeat the rejected answer."
    )


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

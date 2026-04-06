#!/usr/bin/env python3
"""Extract voice exemplar candidates from conversation transcripts.

Reads JSONL transcripts from the Claude Code project directory,
filters to user-authored messages, scores for distinctiveness, and
outputs ranked candidates as JSON.

Usage:
    python scripts/extract_voice_exemplars.py [--output FILE] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
from pathlib import Path
from statistics import stdev


def _detect_transcripts_dir() -> Path:
    """Derive CC project directory from the genesis repo location."""
    # CC project IDs are the repo path with / replaced by -
    repo_root = Path(__file__).resolve().parent.parent
    project_id = str(repo_root).replace("/", "-").lstrip("-")
    return Path.home() / ".claude" / "projects" / f"-{project_id}"


TRANSCRIPTS_DIR = _detect_transcripts_dir()

# Words too common to distinguish voice
STOP_WORDS = frozenset(
    ["a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "can't", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during", "each", "few", "for", "from", "further", "get", "got", "had", "hadn't", "has", "hasn't", "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself", "just", "let", "let's", "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", "that", "that's", "the", "their", "theirs", "them", "themselves", "then", "there", "there's", "these", "they", "they'd", "they'll", "they're", "they've", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't", "what", "what's", "when", "when's", "where", "where's", "which", "while", "who", "who's", "whom", "why", "why's", "will", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves"]
)

# Imperative command verbs that signal "do X" messages, not voice exemplars
COMMAND_VERBS = frozenset(
    ["fix", "run", "do", "create", "update", "delete", "add", "remove", "install", "check", "test", "build", "deploy", "start", "stop", "restart", "commit", "push", "pull", "merge", "rebase", "clone", "configure", "set", "enable", "disable", "read", "write", "edit", "save", "load", "open", "close", "search", "find", "show", "list", "make", "move", "copy", "rename", "clear", "reset", "verify", "confirm"]
)


def extract_user_messages(filepath: Path) -> list[dict]:
    """Extract user text messages from a JSONL transcript file."""
    messages = []
    session_id = filepath.stem

    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            if rec.get("type") != "user":
                continue

            msg = rec.get("message", {})
            if msg.get("role") != "user":
                continue

            content = msg.get("content", "")
            text = ""

            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                # Extract text items from list content
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                text = "\n".join(parts)

            if text:
                messages.append({
                    "session_id": session_id,
                    "text": text.strip(),
                    "timestamp": rec.get("timestamp", ""),
                })

    return messages


def word_count(text: str) -> int:
    """Count words in text."""
    return len(text.split())


def is_command_message(text: str) -> bool:
    """Check if message is a pure command (imperative, not opinion/analysis)."""
    # Short messages are likely commands
    sentences = split_sentences(text)
    if len(sentences) < 2 and word_count(text) < 30:
        return True

    # Check if first word is a command verb
    first_word = text.split()[0].lower().rstrip(".,!?:;")
    if first_word in COMMAND_VERBS and word_count(text) < 50:
        return True

    # Messages that are mostly file paths or code
    code_indicators = text.count("`") + text.count("/home/") + text.count("src/")
    return code_indicators > 5 and word_count(text) < 80


def has_voice_markers(text: str) -> bool:
    """Check if message contains opinion/analysis/narrative markers."""
    markers = [
        r"\bi think\b", r"\bbecause\b", r"\bshould\b", r"\bthe problem is\b",
        r"\bin my experience\b", r"\bwhat if\b", r"\bthe reason\b",
        r"\bi believe\b", r"\bactually\b", r"\bthe key\b", r"\bthe real\b",
        r"\bhonestly\b", r"\bfrankly\b", r"\bhere's (the|my|what)\b",
        r"\bwe need\b", r"\bwe should\b", r"\bthat's (not|why|how|what)\b",
        r"\bi want\b", r"\bi need\b", r"\bthe point is\b",
        r"\bi('m| am) not (sure|sold|convinced)\b",
        r"\bregarding\b", r"\bmy concern\b", r"\bmy question\b",
    ]
    text_lower = text.lower()
    return any(re.search(m, text_lower) for m in markers)


def split_sentences(text: str) -> list[str]:
    """Split text into sentences (simple heuristic)."""
    # Split on sentence-ending punctuation followed by space or end
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p for p in parts if p.strip()]


def compute_type_token_ratio(text: str) -> float:
    """Compute type-token ratio (unique words / total words)."""
    words = [
        w.lower().strip(string.punctuation)
        for w in text.split()
        if w.strip(string.punctuation)
    ]
    if not words:
        return 0.0
    return len(set(words)) / len(words)


def compute_sentence_variation(text: str) -> float:
    """Compute standard deviation of sentence lengths."""
    sentences = split_sentences(text)
    if len(sentences) < 2:
        return 0.0
    lengths = [len(s.split()) for s in sentences]
    return stdev(lengths)


def compute_specificity(text: str) -> int:
    """Count specificity indicators (proper nouns, numbers, concrete refs)."""
    score = 0

    # Numbers (not just single digits)
    numbers = re.findall(r'\b\d{2,}\b', text)
    score += len(numbers)

    # Capitalized words that aren't sentence starters (likely proper nouns)
    words = text.split()
    for i, w in enumerate(words):
        if i == 0:
            continue
        # Check if previous char was sentence-ending
        if i > 0 and words[i - 1][-1] in ".!?":
            continue
        if w[0].isupper() and w.lower() not in STOP_WORDS and len(w) > 1:
            score += 1

    # Technology/tool references
    tech_patterns = [
        r'\b(AWS|GCP|Azure|Kubernetes|K8s|Docker|Terraform|Linux)\b',
        r'\b(Python|TypeScript|JavaScript|Go|Rust)\b',
        r'\b(Claude|GPT|Gemini|OpenAI|Anthropic)\b',
        r'\b(GitHub|Slack|Jira|Linear|Obsidian)\b',
    ]
    for pattern in tech_patterns:
        score += len(re.findall(pattern, text))

    return score


def suggest_medium(text: str, wc: int) -> str:
    """Suggest a medium category for the exemplar."""
    text_lower = text.lower()
    if wc > 200:
        return "longform"
    if any(w in text_lower for w in ["architecture", "infrastructure", "deploy",
                                      "code", "system", "technical", "api"]):
        return "professional"
    return "social"


def suggest_tone(text: str) -> str:
    """Suggest a tone category for the exemplar."""
    text_lower = text.lower()
    if any(w in text_lower for w in ["i think", "i believe", "my view",
                                      "honestly", "frankly"]):
        return "direct"
    if any(w in text_lower for w in ["because", "the reason", "consider",
                                      "analysis", "evaluate"]):
        return "analytical"
    if any(w in text_lower for w in ["what if", "imagine", "could we",
                                      "interesting"]):
        return "reflective"
    return "casual"


def deduplicate(candidates: list[dict], threshold: float = 0.6) -> list[dict]:
    """Remove candidates with >threshold shared uncommon words."""
    result = []
    seen_word_sets: list[set[str]] = []

    for cand in candidates:
        words = {
            w.lower().strip(string.punctuation)
            for w in cand["text"].split()
            if w.lower().strip(string.punctuation) not in STOP_WORDS
            and len(w.strip(string.punctuation)) > 2
        }
        if not words:
            continue

        is_dup = False
        for seen in seen_word_sets:
            overlap = len(words & seen) / min(len(words), len(seen)) if seen else 0
            if overlap > threshold:
                is_dup = True
                break

        if not is_dup:
            result.append(cand)
            seen_word_sets.append(words)

    return result


def main():
    parser = argparse.ArgumentParser(description="Extract voice exemplar candidates")
    parser.add_argument("--output", "-o", help="Output file (default: stdout)")
    parser.add_argument("--limit", "-l", type=int, default=100,
                        help="Max candidates to output (default: 100)")
    parser.add_argument("--min-words", type=int, default=50,
                        help="Minimum word count (default: 50)")
    args = parser.parse_args()

    if not TRANSCRIPTS_DIR.exists():
        print(f"Error: transcript directory not found: {TRANSCRIPTS_DIR}",
              file=sys.stderr)
        sys.exit(1)

    jsonl_files = sorted(TRANSCRIPTS_DIR.glob("*.jsonl"))
    print(f"Processing {len(jsonl_files)} transcript files...", file=sys.stderr)

    # Stage 1 & 2: Parse, filter, content filter
    candidates = []
    total_messages = 0
    for filepath in jsonl_files:
        messages = extract_user_messages(filepath)
        total_messages += len(messages)

        for msg in messages:
            wc = word_count(msg["text"])
            if wc < args.min_words:
                continue
            if is_command_message(msg["text"]):
                continue
            if not has_voice_markers(msg["text"]):
                continue

            candidates.append(msg)

    print(f"Total user messages: {total_messages}", file=sys.stderr)
    print(f"After filtering: {len(candidates)} candidates", file=sys.stderr)

    # Stage 3: Score distinctiveness
    scored = []
    for cand in candidates:
        text = cand["text"]
        wc = word_count(text)
        ttr = compute_type_token_ratio(text)
        sent_var = compute_sentence_variation(text)
        specificity = compute_specificity(text)

        # Composite score: weighted combination
        # TTR (0-1): higher = more diverse vocabulary
        # Sent var: higher = more natural variation (cap at ~15 for normalization)
        # Specificity: higher = more concrete (cap at ~10 for normalization)
        score = (ttr * 0.4) + (min(sent_var, 15) / 15 * 0.3) + (min(specificity, 10) / 10 * 0.3)

        scored.append({
            "session_id": cand["session_id"],
            "text": text,
            "timestamp": cand.get("timestamp", ""),
            "word_count": wc,
            "ttr": round(ttr, 3),
            "sentence_std": round(sent_var, 2),
            "specificity": specificity,
            "score": round(score, 3),
            "suggested_medium": suggest_medium(text, wc),
            "suggested_tone": suggest_tone(text),
        })

    # Stage 4: Rank and deduplicate
    scored.sort(key=lambda x: x["score"], reverse=True)
    deduped = deduplicate(scored)

    # Stage 5: Output
    output = []
    for i, cand in enumerate(deduped[:args.limit], 1):
        cand["id"] = i
        output.append(cand)

    print(f"After dedup: {len(deduped)} unique candidates", file=sys.stderr)
    print(f"Outputting top {len(output)}", file=sys.stderr)

    result = json.dumps(output, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(result, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(result)


if __name__ == "__main__":
    main()

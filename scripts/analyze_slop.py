#!/usr/bin/env python3
"""AI writing pattern analyzer — score, analyze, suggest, fix, compare, stats.

Detects AI-generated text patterns using vocabulary tiers, structural pattern
matching, and statistical analysis (burstiness, TTR, trigram repetition,
Flesch-Kincaid). Built from published academic techniques, not ported from
any external codebase.

Usage:
    python scripts/analyze_slop.py score < draft.md
    python scripts/analyze_slop.py analyze draft.md
    python scripts/analyze_slop.py suggest draft.md
    python scripts/analyze_slop.py fix draft.md [-o cleaned.md] [-a]
    python scripts/analyze_slop.py compare draft.md [-a]
    python scripts/analyze_slop.py stats draft.md
    python scripts/analyze_slop.py analyze --json draft.md
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

# ── Vocabulary Tiers ─────────────────────────────────────────────────────────
# Based on Wikipedia:Signs of AI writing and Copyleaks research.
# Tier 1: dead giveaways (5-20x more common in AI text than human).
# Tier 2: suspicious when clustered (fine alone, damning in groups).
# Tier 3: context-dependent (only at high density).

TIER_1 = [
    "delve", "delving", "delved", "delves", "tapestry", "vibrant", "crucial",
    "comprehensive", "intricate", "intricacies", "pivotal", "testament",
    "bustling", "nestled", "realm", "meticulous", "meticulously",
    "complexities", "embark", "embarking", "embarked", "robust",
    "showcasing", "showcase", "showcased", "showcases", "underscores",
    "underscoring", "underscored", "fostering", "foster", "fostered",
    "fosters", "seamless", "seamlessly", "groundbreaking", "renowned",
    "synergy", "synergies", "leverage", "leveraging", "leveraged",
    "garner", "garnered", "garnering", "interplay", "enduring",
    "enhance", "enhanced", "enhancing", "enhancement", "additionally",
    "daunting", "ever-evolving", "game-changer", "game-changing",
    "underscore", "helmed", "prowess",
]

TIER_2 = [
    "furthermore", "moreover", "notably", "consequently", "subsequently",
    "accordingly", "nonetheless", "henceforth", "indeed", "fundamentally",
    "inherently", "profoundly", "encompassing", "encompasses", "encompassed",
    "endeavour", "endeavor", "elevate", "elevated", "elevating",
    "streamline", "streamlined", "harness", "harnessing", "harnessed",
    "unleash", "unleashing", "revolutionize", "revolutionizing",
    "transformative", "transformation", "paramount", "multifaceted",
    "spearhead", "spearheading", "bolster", "bolstering", "catalyze",
    "catalyst", "cornerstone", "reimagine", "reimagining", "empower",
    "empowering", "empowerment", "navigate", "navigating", "poised",
    "myriad", "nuanced", "nuance", "paradigm", "holistic", "utilize",
    "utilizing", "utilization", "facilitate", "facilitated", "facilitating",
    "elucidate", "illuminate", "illuminating", "invaluable", "cutting-edge",
    "innovative", "innovation", "impactful", "agile", "scalable",
    "proactive", "optimize", "optimizing", "resonate", "resonating",
    "cultivate", "cultivating", "juxtapose", "juxtaposition",
    "proliferate", "proliferation", "burgeoning", "nascent", "ubiquitous",
    "plethora", "quintessential", "overarching", "underpinning",
]

TIER_3 = [
    "significant", "significantly", "important", "importantly", "effective",
    "effectively", "efficient", "efficiently", "diverse", "diversity",
    "unique", "uniquely", "vital", "critical", "essential", "valuable",
    "notable", "remarkable", "substantial", "noteworthy", "prominent",
    "influential", "thoughtful", "insightful", "meaningful", "purposeful",
    "deliberate", "strategic", "integral", "indispensable", "instrumental",
    "imperative", "exemplary", "sophisticated", "compelling", "captivating",
    "exceptional", "extraordinary", "unprecedented", "monumental",
    "trailblazing", "visionary", "world-class", "state-of-the-art",
]

# ── AI Phrases ───────────────────────────────────────────────────────────────

FILLER_REPLACEMENTS = [
    (r"\bin order to\b", "to"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bat this point in time\b", "now"),
    (r"\bin the event that\b", "if"),
    (r"\bhas the ability to\b", "can"),
    (r"\bfor the purpose of\b", "to"),
    (r"\bfirst and foremost\b", "first"),
    (r"\bin light of the fact that\b", "because"),
    (r"\bin the realm of\b", "in"),
    (r"\bit is important to note that\b", ""),
    (r"\bit is worth noting that\b", ""),
    (r"\bit should be noted that\b", ""),
]

COPULA_REPLACEMENTS = [
    (r"\bserves as a\b", "is a"),
    (r"\bserves as an\b", "is an"),
    (r"\bserves as the\b", "is the"),
    (r"\bstands as a\b", "is a"),
    (r"\bstands as an\b", "is an"),
    (r"\bstands as the\b", "is the"),
    (r"\bboasts a\b", "has a"),
    (r"\bboasts an\b", "has an"),
    (r"\bboasts the\b", "has the"),
    (r"\bfeatures a\b", "has a"),
    (r"\bfeatures an\b", "has an"),
    (r"\bfeatures the\b", "has the"),
    (r"\butilize\b", "use"),
    (r"\butilizes\b", "uses"),
    (r"\butilizing\b", "using"),
    (r"\bleverage\b", "use"),
    (r"\bleverages\b", "uses"),
    (r"\bleveraging\b", "using"),
    (r"\bfacilitate\b", "help"),
    (r"\bfacilitates\b", "helps"),
    (r"\bfacilitating\b", "helping"),
]

CHATBOT_SENTENCES = [
    "I hope this helps",
    "Let me know if",
    "Would you like me to",
    "Feel free to",
    "Don't hesitate to",
    "Happy to help",
    "As an AI",
    "As a language model",
    "Is there anything else",
]

CHATBOT_OPENERS = [
    r"^(Here is|Here's) (a |an |the )?(comprehensive |brief |quick )?(overview|summary|breakdown|list|guide|explanation|look)[^.]*\.\s*",
    r"^(Of course|Certainly|Absolutely|Sure)!\s*",
    r"^(Great|Excellent|Good|Wonderful|Fantastic) question!\s*",
    r"^(That's|That is) a (great|excellent|good|wonderful|fantastic) (question|point)!\s*",
]

OPENER_REMOVALS = [
    r"\bFurthermore,\s*",
    r"\bMoreover,\s*",
    r"\bAdditionally,\s*",
    r"\bIn conclusion,\s*",
    r"\bTo summarize,\s*",
    r"\bIn summary,\s*",
    r"\bOverall,\s*",
]

SIGNIFICANCE_PHRASES = [
    r"marking a pivotal", r"pivotal moment", r"pivotal role",
    r"crucial role", r"vital role", r"significant role",
    r"is a testament", r"serves as a testament", r"serves as a reminder",
    r"reflects broader", r"broader trends", r"evolving landscape",
    r"setting the stage for", r"key turning point", r"indelible mark",
    r"deeply rooted", r"enduring legacy", r"lasting impact",
    r"underscores the importance", r"shaping the future",
    r"the evolution of", r"rich tapestry", r"stands as a beacon",
    r"paving the way", r"charting a course",
]

SYCOPHANTIC_PHRASES = [
    r"\bgreat question\b", r"\bexcellent (question|point|observation)\b",
    r"\byou're absolutely right\b",
    r"\byou raise a (great|good|excellent|valid) point\b",
]

HEDGING_STACKS = [
    r"\bcould potentially\b", r"\bmight possibly\b", r"\bcould possibly\b",
    r"\bperhaps potentially\b", r"\bmay potentially\b",
]

GENERIC_CONCLUSIONS = [
    r"\bthe future (looks|is|remains) bright\b",
    r"\bexciting times (lie|lay|are) ahead\b",
    r"\bcontinue (this|their|our|the) journey\b",
    r"\bjourney toward(s)? (excellence|success|greatness)\b",
    r"\bthe possibilities are (endless|limitless|infinite)\b",
    r"\bpoised for (growth|success|greatness)\b",
    r"\bonly time will tell\b",
]

VAGUE_ATTRIBUTIONS = [
    r"\bexperts (believe|argue|say|suggest|note)\b",
    r"\bindustry (reports|observers|experts|analysts)\b",
    r"\bstudies (show|suggest|indicate)\b",
    r"\bresearch (shows|suggests|indicates)\b",
    r"\bwidely (regarded|considered|recognized)\b",
]

CITATION_ARTIFACTS = [
    r"\[oai_citation:\d+[^\]]*\]\([^)]*\)",
    r":contentReference\[oaicite:\d+\]\{[^}]*\}",
    r"\boaicite\b",
    r"\bturn0search\d+",
    r"\bturn0image\d+",
    r"\?utm_source=(chatgpt\.com|openai)",
]


# ── Pattern Detectors ────────────────────────────────────────────────────────

def _find_matches(text: str, pattern: str, flags: int = re.IGNORECASE) -> list[dict]:
    """Find all regex matches with line numbers."""
    results = []
    for i, line in enumerate(text.split("\n"), 1):
        for m in re.finditer(pattern, line, flags):
            results.append({
                "match": m.group(),
                "line": i,
                "column": m.start() + 1,
            })
    return results


def _scan_word_list(text: str, words: list[str]) -> list[dict]:
    """Scan text for words from a list. Returns matches with line numbers."""
    results = []
    for word in words:
        escaped = re.escape(word)
        pattern = rf"\b{escaped}\b"
        results.extend(_find_matches(text, pattern))
    return results


def _word_count(text: str) -> int:
    return len(text.split())


PATTERNS = [
    {
        "id": 1, "name": "Significance inflation", "category": "content", "weight": 4,
        "description": "Inflated claims about significance, legacy, or broader trends.",
        "detect": lambda text: [
            {**m, "suggestion": "Remove inflated significance claim. State concrete facts."}
            for p in SIGNIFICANCE_PHRASES for m in _find_matches(text, p)
        ],
    },
    {
        "id": 2, "name": "Superficial -ing analyses", "category": "content", "weight": 4,
        "description": "Trailing -ing participial phrases that fake analytical depth.",
        "detect": lambda text: [
            {**m, "suggestion": "Remove trailing -ing phrase. Give the point its own sentence."}
            for m in _find_matches(
                text,
                r",\s*(highlighting|underscoring|emphasizing|ensuring|reflecting|symbolizing|"
                r"contributing to|cultivating|fostering|encompassing|showcasing|demonstrating|"
                r"illustrating|representing|solidifying|reinforcing)\b[^.]{5,}",
            )
        ],
    },
    {
        "id": 3, "name": "Vague attributions", "category": "content", "weight": 4,
        "description": "Claims attributed to unnamed experts or vague sources.",
        "detect": lambda text: [
            {**m, "suggestion": "Name the specific source, or remove the claim."}
            for p in VAGUE_ATTRIBUTIONS for m in _find_matches(text, p)
        ],
    },
    {
        "id": 4, "name": "AI vocabulary (Tier 1)", "category": "language", "weight": 5,
        "description": "Dead-giveaway AI words (5-20x more common in AI text).",
        "detect": lambda text: [
            {**m, "suggestion": f'Tier 1 AI word: "{m["match"]}". Use a simpler alternative.'}
            for m in _scan_word_list(text, TIER_1)
        ],
    },
    {
        "id": 5, "name": "AI vocabulary (Tier 2)", "category": "language", "weight": 3,
        "description": "Suspicious words when clustered (fine alone, damning in groups).",
        "detect": lambda text: (
            lambda matches: matches if len(matches) >= 2 else []
        )([
            {**m, "suggestion": f'Tier 2 AI word: "{m["match"]}". Consider a plainer alternative.'}
            for m in _scan_word_list(text, TIER_2)
        ]),
    },
    {
        "id": 6, "name": "AI vocabulary (Tier 3 density)", "category": "language", "weight": 2,
        "description": "Common words that signal AI only at high density (>3% of words).",
        "detect": lambda text: (
            lambda matches, wc: matches if wc > 50 and len(matches) / max(wc / 50, 1) > 1.5 else []
        )(
            [{**m, "suggestion": f'High-density Tier 3 word: "{m["match"]}".'}
             for m in _scan_word_list(text, TIER_3)],
            _word_count(text),
        ),
    },
    {
        "id": 7, "name": "Copula avoidance", "category": "language", "weight": 3,
        "description": 'Using "serves as", "boasts", "features" instead of "is", "has".',
        "detect": lambda text: [
            {**m, "suggestion": 'Use simple "is", "are", or "has" instead.'}
            for p, _ in COPULA_REPLACEMENTS for m in _find_matches(text, p)
        ],
    },
    {
        "id": 8, "name": "Negative parallelisms", "category": "language", "weight": 3,
        "description": '"Not just X, it\'s Y" overused rhetorical frame.',
        "detect": lambda text: [
            {**m, "suggestion": "Rewrite directly. State what it IS, not what it 'isn't just'."}
            for m in _find_matches(
                text,
                r"\b(it'?s|this is) not (just|merely|only|simply) .{3,60}(,|;|\u2014)\s*(it'?s|this is|but)\b",
            )
        ] + [
            {**m, "suggestion": 'Simplify. Remove the "not only...but also" frame.'}
            for m in _find_matches(text, r"\bnot only .{3,60} but (also )?\b")
        ],
    },
    {
        "id": 9, "name": "Synonym cycling", "category": "language", "weight": 2,
        "description": "Referring to the same thing by different names in consecutive sentences.",
        "detect": lambda text: _detect_synonym_cycling(text),
    },
    {
        "id": 10, "name": "Chatbot artifacts", "category": "communication", "weight": 5,
        "description": 'Leftover chatbot phrases: "I hope this helps!", "Great question!".',
        "detect": lambda text: [
            {**m, "suggestion": "Remove chatbot artifact."}
            for phrase in CHATBOT_SENTENCES
            for m in _find_matches(text, re.escape(phrase))
        ] + [
            {**m, "suggestion": "Remove sycophantic phrase."}
            for p in SYCOPHANTIC_PHRASES for m in _find_matches(text, p)
        ],
    },
    {
        "id": 11, "name": "Filler phrases", "category": "filler", "weight": 3,
        "description": 'Wordy filler: "in order to" -> "to", "due to the fact that" -> "because".',
        "detect": lambda text: [
            {**m, "suggestion": f'Replace with: "{repl}"' if repl else "Remove this phrase."}
            for p, repl in FILLER_REPLACEMENTS for m in _find_matches(text, p)
        ],
    },
    {
        "id": 12, "name": "Excessive hedging", "category": "filler", "weight": 3,
        "description": "Stacking qualifiers: 'could potentially possibly'.",
        "detect": lambda text: [
            {**m, "suggestion": "Use a single qualifier. 'Could potentially' -> 'could'."}
            for p in HEDGING_STACKS for m in _find_matches(text, p)
        ],
    },
    {
        "id": 13, "name": "Generic conclusions", "category": "filler", "weight": 3,
        "description": 'Vague upbeat endings: "The future looks bright".',
        "detect": lambda text: [
            {**m, "suggestion": "End with a specific fact or plan, not a platitude."}
            for p in GENERIC_CONCLUSIONS for m in _find_matches(text, p)
        ],
    },
    {
        "id": 14, "name": "Citation artifacts", "category": "communication", "weight": 5,
        "description": "Raw AI citation bugs (oaicite, contentReference, turn0search).",
        "detect": lambda text: [
            {**m, "suggestion": "Delete this raw AI artifact."}
            for p in CITATION_ARTIFACTS for m in _find_matches(text, p)
        ],
    },
    {
        "id": 15, "name": "Curly quotes", "category": "style", "weight": 1,
        "description": "ChatGPT-style Unicode curly quotes instead of straight quotes.",
        "detect": lambda text: [
            {**m, "suggestion": "Replace curly quotes with straight quotes."}
            for m in _find_matches(text, "[\u201c\u201d\u2018\u2019]")
        ],
    },
    {
        "id": 16, "name": "Em dash overuse", "category": "style", "weight": 2,
        "description": "LLMs overuse em dashes as a crutch for punchy writing.",
        "detect": lambda text: (
            lambda matches, wc: matches if len(matches) >= 2 and wc > 0 and len(matches) / (wc / 100) > 1.0 else []
        )(
            [{**m, "suggestion": "Replace with comma, period, or parentheses."}
             for m in _find_matches(text, "\u2014")],
            _word_count(text),
        ),
    },
    {
        "id": 17, "name": "Boldface overuse", "category": "style", "weight": 2,
        "description": "Mechanical emphasis of phrases in bold.",
        "detect": lambda text: (
            lambda matches: matches if len(matches) >= 3 else []
        )([
            {**m, "suggestion": "Remove bold — let the writing carry the weight."}
            for m in _find_matches(text, r"\*\*[^*]+\*\*")
        ]),
    },
    {
        "id": 18, "name": "Inline-header lists", "category": "style", "weight": 3,
        "description": "List items starting with bolded headers and colons.",
        "detect": lambda text: (
            lambda matches: matches if len(matches) >= 2 else []
        )([
            {**m, "suggestion": "Convert to prose or a simpler list."}
            for m in _find_matches(text, r"^[*-]\s+\*\*[^*]+:\*\*\s", re.MULTILINE)
        ]),
    },
]


def _detect_synonym_cycling(text: str) -> list[dict]:
    """Detect synonym cycling across consecutive sentences."""
    synonym_sets = [
        ["protagonist", "main character", "central figure", "hero", "lead character"],
        ["company", "firm", "organization", "enterprise", "corporation"],
        ["building", "structure", "edifice", "facility", "complex"],
        ["problem", "challenge", "issue", "obstacle", "hurdle", "difficulty"],
        ["solution", "approach", "methodology", "framework", "strategy"],
    ]
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    results = []
    for syns in synonym_sets:
        for i in range(len(sentences) - 1):
            found = []
            for j in range(i, min(i + 4, len(sentences))):
                lower = sentences[j].lower()
                for syn in syns:
                    if syn in lower and syn not in found:
                        found.append(syn)
            if len(found) >= 3:
                results.append({
                    "match": f"Synonym cycling: {' -> '.join(found)}",
                    "line": text[:text.index(sentences[i])].count("\n") + 1,
                    "column": 1,
                    "suggestion": f'Pick one term and stick with it. Found: {", ".join(found)}',
                })
                break
    return results


# ── Statistics Engine ────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, handling abbreviations."""
    dot_placeholder = "\u2024"  # one-dot leader, not a period
    cleaned = re.sub(r"\b(Mr|Mrs|Ms|Dr|Prof|etc|vs)\.", rf"\1{dot_placeholder}", text)
    cleaned = re.sub(r"\b([A-Z])\.", rf"\1{dot_placeholder}", cleaned)
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\u201c])|(?<=[.!?])$', cleaned)
    return [p.replace(dot_placeholder, ".").strip() for p in parts if p.strip()]


def _tokenize(text: str) -> list[str]:
    """Tokenize into lowercase words."""
    return [w for w in re.sub(r"[^\w\s'-]", " ", text.lower()).split() if w]


def _estimate_syllables(word: str) -> int:
    """Estimate syllable count (English heuristic)."""
    word = re.sub(r"[^a-z]", "", word.lower())
    if len(word) <= 3:
        return 1
    vowel_groups = re.findall(r"[aeiouy]+", word)
    count = len(vowel_groups) if vowel_groups else 1
    if word.endswith("e") and not word.endswith("le"):
        count -= 1
    return max(count, 1)


FUNCTION_WORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
    "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
    "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
    "when", "make", "can", "like", "time", "no", "just", "him", "know",
    "take", "people", "into", "year", "your", "good", "some", "could",
    "them", "see", "other", "than", "then", "now", "look", "only", "come",
    "its", "over", "think", "also", "back", "after", "use", "two", "how",
    "our", "work", "first", "well", "way", "even", "new", "want",
    "because", "any", "these", "give", "day", "most", "us",
}


def compute_stats(text: str) -> dict:
    """Compute text statistics for AI detection."""
    words = _tokenize(text)
    sentences = _split_sentences(text)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    if not words:
        return _empty_stats()

    wc = len(words)
    unique = set(words)
    ttr = len(unique) / wc

    # Sentence stats
    sent_lengths = [len(_tokenize(s)) for s in sentences if _tokenize(s)]
    sent_count = len(sent_lengths)
    avg_sent = sum(sent_lengths) / sent_count if sent_count else 0
    sent_std = 0.0
    burstiness = 0.0
    sent_variation = 0.0

    if sent_count > 1:
        variance = sum((slen - avg_sent) ** 2 for slen in sent_lengths) / sent_count
        sent_std = math.sqrt(variance)
        sent_variation = sent_std / avg_sent if avg_sent > 0 else 0

        # Burstiness: consecutive sentence length differences
        consec_sum = sum(
            abs(sent_lengths[i] - sent_lengths[i - 1])
            for i in range(1, len(sent_lengths))
        )
        burstiness = (consec_sum / (len(sent_lengths) - 1)) / avg_sent if avg_sent > 0 else 0

    # Function word ratio
    fw_count = sum(1 for w in words if w in FUNCTION_WORDS)
    fw_ratio = fw_count / wc

    # Trigram repetition
    trigram_rep = _trigram_repetition(words)

    # Flesch-Kincaid
    syllables = sum(_estimate_syllables(w) for w in words)
    fk = (0.39 * (wc / max(sent_count, 1)) + 11.8 * (syllables / wc) - 15.59) if sent_count > 0 else 0

    # Paragraph stats
    para_count = len(paragraphs)
    avg_para = sum(len(_tokenize(p)) for p in paragraphs) / para_count if para_count else 0

    return {
        "word_count": wc,
        "unique_words": len(unique),
        "sentence_count": sent_count,
        "paragraph_count": para_count,
        "avg_word_length": round(sum(len(w) for w in words) / wc, 2),
        "avg_sentence_length": round(avg_sent, 2),
        "sentence_std_dev": round(sent_std, 2),
        "sentence_variation": round(sent_variation, 3),
        "burstiness": round(burstiness, 3),
        "type_token_ratio": round(ttr, 3),
        "function_word_ratio": round(fw_ratio, 3),
        "trigram_repetition": round(trigram_rep, 3),
        "avg_paragraph_length": round(avg_para, 2),
        "flesch_kincaid": round(fk, 1),
    }


def _trigram_repetition(words: list[str]) -> float:
    """Fraction of trigrams that appear more than once."""
    if len(words) < 3:
        return 0.0
    trigrams: dict[str, int] = {}
    for i in range(len(words) - 2):
        gram = f"{words[i]} {words[i+1]} {words[i+2]}"
        trigrams[gram] = trigrams.get(gram, 0) + 1
    if not trigrams:
        return 0.0
    repeated = sum(1 for c in trigrams.values() if c > 1)
    return repeated / len(trigrams)


def _empty_stats() -> dict:
    return {
        "word_count": 0, "unique_words": 0, "sentence_count": 0,
        "paragraph_count": 0, "avg_word_length": 0, "avg_sentence_length": 0,
        "sentence_std_dev": 0, "sentence_variation": 0, "burstiness": 0,
        "type_token_ratio": 0, "function_word_ratio": 0, "trigram_repetition": 0,
        "avg_paragraph_length": 0, "flesch_kincaid": 0,
    }


def compute_uniformity_score(stats: dict) -> int:
    """Score how uniform/AI-like the text statistics are. 0-100."""
    score = 0
    if stats["burstiness"] < 0.2:
        score += 25
    elif stats["burstiness"] < 0.35:
        score += 18
    elif stats["burstiness"] < 0.5:
        score += 10

    if stats["sentence_variation"] < 0.2:
        score += 25
    elif stats["sentence_variation"] < 0.35:
        score += 18
    elif stats["sentence_variation"] < 0.5:
        score += 10

    if stats["word_count"] > 100:
        if stats["type_token_ratio"] < 0.35:
            score += 20
        elif stats["type_token_ratio"] < 0.45:
            score += 12

    if stats["trigram_repetition"] > 0.15:
        score += 15
    elif stats["trigram_repetition"] > 0.1:
        score += 10

    if stats["paragraph_count"] >= 3 and stats["sentence_std_dev"] < 3 and stats["avg_sentence_length"] > 10:
        score += 15

    return min(score, 100)


# ── Analysis Engine ──────────────────────────────────────────────────────────

def analyze(text: str) -> dict:
    """Full analysis: patterns + statistics + composite score."""
    if not text or not text.strip():
        return {"score": 0, "pattern_score": 0, "uniformity_score": 0,
                "total_matches": 0, "word_count": 0, "stats": _empty_stats(),
                "findings": [], "summary": "No text provided."}

    text = text.strip()
    wc = _word_count(text)
    stats = compute_stats(text)
    uniformity = compute_uniformity_score(stats) if wc >= 20 and stats["sentence_count"] >= 3 else 0

    findings = []
    for pattern in PATTERNS:
        matches = pattern["detect"](text)
        if matches:
            findings.append({
                "pattern_id": pattern["id"],
                "pattern_name": pattern["name"],
                "category": pattern["category"],
                "description": pattern["description"],
                "weight": pattern["weight"],
                "match_count": len(matches),
                "matches": matches[:5],
            })

    # Pattern score: density + breadth + category diversity
    pattern_score = _calculate_pattern_score(findings, wc)

    # Composite: 70% pattern + 30% uniformity
    if not findings:
        composite = min(round(uniformity * 0.15), 15)
    else:
        composite = min(round(pattern_score * 0.7 + uniformity * 0.3), 100)

    total_matches = sum(f["match_count"] for f in findings)

    level = (
        "heavily AI-generated" if composite >= 70 else
        "moderately AI-influenced" if composite >= 45 else
        "lightly AI-touched" if composite >= 20 else
        "mostly human-sounding"
    )

    top_patterns = sorted(findings, key=lambda f: f["match_count"] * f["weight"], reverse=True)[:3]
    summary = f"Score: {composite}/100 ({level}). {total_matches} matches across {len(findings)} patterns in {wc} words."
    if top_patterns:
        summary += " Top: " + ", ".join(f["pattern_name"] for f in top_patterns) + "."

    return {
        "score": composite,
        "pattern_score": pattern_score,
        "uniformity_score": uniformity,
        "total_matches": total_matches,
        "word_count": wc,
        "stats": stats,
        "findings": findings,
        "summary": summary,
    }


def _calculate_pattern_score(findings: list[dict], wc: int) -> int:
    """Pattern-based score component (0-100)."""
    if not findings or wc == 0:
        return 0
    weighted = sum(f["match_count"] * f["weight"] for f in findings)
    density = (weighted / wc) * 100
    density_score = min(math.log2(density + 1) * 13, 65)
    breadth = min(len(findings) * 2, 20)
    categories = len({f["category"] for f in findings})
    cat_bonus = min(categories * 3, 15)
    return min(round(density_score + breadth + cat_bonus), 100)


# ── Auto-Fix Engine ──────────────────────────────────────────────────────────

def auto_fix(text: str, aggressive: bool = False) -> tuple[str, list[str]]:
    """Apply safe mechanical fixes. Returns (fixed_text, list_of_fixes)."""
    result = text
    fixes = []

    # Citation artifacts
    for p in CITATION_ARTIFACTS:
        if re.search(p, result, re.IGNORECASE):
            result = re.sub(p, "", result, flags=re.IGNORECASE)
            fixes.append(f"Removed citation artifact: {p[:30]}")

    # Bold markdown stripping (only when 3+ bold phrases, matching analyzer threshold)
    bold_matches = re.findall(r"\*\*[^*]+\*\*", result)
    if len(bold_matches) >= 3:
        result = re.sub(r"\*\*([^*]+)\*\*", r"\1", result)
        fixes.append(f"Stripped {len(bold_matches)} bold markdown phrases")

    # Curly quotes
    if re.search("[\u201c\u201d]", result):
        result = re.sub("[\u201c\u201d]", '"', result)
        fixes.append("Replaced curly double quotes")
    if re.search("[\u2018\u2019]", result):
        result = re.sub("[\u2018\u2019]", "'", result)
        fixes.append("Replaced curly single quotes")

    # Chatbot sentence removal
    for phrase in CHATBOT_SENTENCES:
        escaped = re.escape(phrase)
        regex = re.compile(r"[^.!?\n]*" + escaped + r"[^.!?\n]*[.!?]?\s*", re.IGNORECASE)
        if regex.search(result):
            result = regex.sub("", result)
            fixes.append(f'Removed "{phrase}" sentence')

    # Chatbot openers
    for p in CHATBOT_OPENERS:
        if re.search(p, result, re.IGNORECASE | re.MULTILINE):
            result = re.sub(p, "", result, flags=re.IGNORECASE | re.MULTILINE)
            fixes.append("Removed chatbot opening")

    # Copula replacements
    for p, repl in COPULA_REPLACEMENTS:
        if re.search(p, result, re.IGNORECASE):
            result = re.sub(p, repl, result, flags=re.IGNORECASE)
            fixes.append(f'"{p[2:-2]}" -> "{repl}"')

    # Filler replacements
    for p, repl in FILLER_REPLACEMENTS:
        if re.search(p, result, re.IGNORECASE):
            result = re.sub(p, repl, result, flags=re.IGNORECASE)
            fix_label = f'Replaced filler with "{repl}"' if repl else "Removed filler phrase"
            fixes.append(fix_label)

    # Opener removals
    for p in OPENER_REMOVALS:
        if re.search(p, result):
            result = re.sub(p, "", result)
            fixes.append(f"Removed opener: {p[2:p.index(',')]}")

    # Aggressive mode
    if aggressive:
        ing_words = ["highlighting", "underscoring", "emphasizing", "showcasing", "fostering"]
        for word in ing_words:
            regex = re.compile(rf",?\s*{word}\s+[^,.]+[,.]", re.IGNORECASE)
            if regex.search(result):
                result = regex.sub(". ", result)
                fixes.append(f'Simplified "{word}" clause')

        em_count = result.count("\u2014") + len(re.findall(r"\s+--\s+", result))
        if em_count > 2:
            result = re.sub(r"\s*\u2014\s*", ", ", result)
            result = re.sub(r"\s+--\s+", ", ", result)
            fixes.append(f"Replaced {em_count} em dashes with commas")

    # Cleanup
    result = re.sub(r" +", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r",\s*,", ",", result)
    result = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), result)
    result = result.strip()

    return result, fixes


# ── Suggestion Engine ────────────────────────────────────────────────────────

def suggest(text: str) -> dict:
    """Generate prioritized suggestions."""
    result = analyze(text)
    critical, important, minor = [], [], []

    for finding in result["findings"]:
        for m in finding["matches"]:
            entry = {
                "pattern": finding["pattern_name"],
                "pattern_id": finding["pattern_id"],
                "weight": finding["weight"],
                "text": m["match"][:80],
                "line": m["line"],
                "suggestion": m.get("suggestion", ""),
            }
            if finding["weight"] >= 4:
                critical.append(entry)
            elif finding["weight"] >= 2:
                important.append(entry)
            else:
                minor.append(entry)

    return {
        "score": result["score"],
        "total_issues": result["total_matches"],
        "word_count": result["word_count"],
        "critical": critical,
        "important": important,
        "minor": minor,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _read_input(args) -> str:
    """Read text from file or stdin."""
    if args.file:
        try:
            return Path(args.file).read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        except UnicodeDecodeError:
            print(f"Error: File is not valid UTF-8: {args.file}", file=sys.stderr)
            sys.exit(1)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print("Error: No input. Pipe text or provide a file.", file=sys.stderr)
    sys.exit(1)


def _score_badge(s: int) -> str:
    if s <= 25:
        return f"[HUMAN] {s}/100"
    if s <= 50:
        return f"[LIGHT] {s}/100"
    if s <= 75:
        return f"[MODERATE] {s}/100"
    return f"[AI] {s}/100"


def _format_stats(stats: dict) -> str:
    lines = [
        "", "  TEXT STATISTICS",
        "  " + "-" * 50,
        f"  Words: {stats['word_count']}  |  Sentences: {stats['sentence_count']}  |  Paragraphs: {stats['paragraph_count']}",
        f"  Avg sentence length: {stats['avg_sentence_length']} words (std: {stats['sentence_std_dev']})",
        f"  Burstiness: {stats['burstiness']}  {'(human-like)' if stats['burstiness'] >= 0.5 else '(AI-like)' if stats['burstiness'] < 0.25 else '(moderate)'}",
        f"  Type-token ratio: {stats['type_token_ratio']}  {'(diverse)' if stats['type_token_ratio'] >= 0.6 else '(repetitive)' if stats['type_token_ratio'] < 0.4 else '(moderate)'}",
        f"  Trigram repetition: {stats['trigram_repetition']}  {'(high — AI-like)' if stats['trigram_repetition'] > 0.1 else '(normal)'}",
        f"  Flesch-Kincaid: grade {stats['flesch_kincaid']}",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI writing pattern analyzer")
    parser.add_argument("command", choices=["score", "analyze", "suggest", "fix", "compare", "stats"])
    parser.add_argument("file", nargs="?", help="Input file (or pipe via stdin)")
    parser.add_argument("-o", "--output", help="Output file for fix command")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--aggressive", "-a", action="store_true", help="Aggressive transforms")
    args = parser.parse_args()

    text = _read_input(args)
    if not text.strip():
        print("Error: Empty input.", file=sys.stderr)
        sys.exit(1)

    if args.command == "score":
        result = analyze(text)
        if args.json:
            print(json.dumps({"score": result["score"]}))
        else:
            print(_score_badge(result["score"]))

    elif args.command == "analyze":
        result = analyze(text)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\n  Score: {_score_badge(result['score'])}")
            print(f"  {result['summary']}")
            print(_format_stats(result["stats"]))
            if result["findings"]:
                print("  FINDINGS")
                print("  " + "-" * 50)
                for f in result["findings"]:
                    print(f"  [{f['pattern_id']}] {f['pattern_name']} (x{f['match_count']}, weight: {f['weight']})")
                    for m in f["matches"]:
                        preview = m["match"][:80]
                        print(f"    L{m['line']}: \"{preview}\"")
                        if m.get("suggestion"):
                            print(f"      -> {m['suggestion']}")
                print()

    elif args.command == "suggest":
        result = suggest(text)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"\n  Score: {_score_badge(result['score'])}  ({result['total_issues']} issues)")
            if result["critical"]:
                print("\n  CRITICAL (dead giveaways)")
                for s in result["critical"]:
                    print(f"    L{s['line']}: [{s['pattern']}] \"{s['text']}\"")
                    print(f"      -> {s['suggestion']}")
            if result["important"]:
                print("\n  IMPORTANT (noticeable patterns)")
                for s in result["important"]:
                    print(f"    L{s['line']}: [{s['pattern']}] \"{s['text']}\"")
                    print(f"      -> {s['suggestion']}")
            if result["minor"]:
                print("\n  MINOR (subtle tells)")
                for s in result["minor"]:
                    print(f"    L{s['line']}: [{s['pattern']}] \"{s['text']}\"")
                    print(f"      -> {s['suggestion']}")
            print()

    elif args.command == "fix":
        fixed, fixes = auto_fix(text, aggressive=args.aggressive)
        if args.json:
            print(json.dumps({"text": fixed, "fixes": fixes}, indent=2, ensure_ascii=False))
        else:
            if fixes:
                print(f"\n  Applied {len(fixes)} fixes:")
                for f in fixes:
                    print(f"    + {f}")
            else:
                print("\n  No mechanical fixes needed.")
            print(f"\n{'=' * 60}\n")
            print(fixed)
            if args.output:
                Path(args.output).write_text(fixed, encoding="utf-8")
                print(f"\n  Saved to {args.output}")
            print()

    elif args.command == "compare":
        before = analyze(text)
        fixed, fixes = auto_fix(text, aggressive=args.aggressive)
        after = analyze(fixed)
        if args.json:
            print(json.dumps({"before": before, "after": after, "fixes": fixes}, indent=2, ensure_ascii=False))
        else:
            print("\n  BEFORE -> AFTER COMPARISON")
            print("  " + "-" * 50)
            score_delta = after["score"] - before["score"]
            issue_delta = after["total_matches"] - before["total_matches"]
            print(f"  Score:   {before['score']}/100  ->  {after['score']}/100  ({score_delta:+d})")
            print(f"  Issues:  {before['total_matches']}  ->  {after['total_matches']}  ({issue_delta:+d})")
            print(f"  Words:   {before['word_count']}  ->  {after['word_count']}")
            if fixes:
                print(f"\n  Transforms ({len(fixes)}):")
                for f in fixes:
                    print(f"    + {f}")
            reduction = before["total_matches"] - after["total_matches"]
            if reduction > 0 and before["total_matches"] > 0:
                pct = round(reduction / before["total_matches"] * 100)
                print(f"\n  Reduced {reduction} issues ({pct}% improvement)")
            print()

    elif args.command == "stats":
        stats = compute_stats(text)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(_format_stats(stats))


if __name__ == "__main__":
    main()

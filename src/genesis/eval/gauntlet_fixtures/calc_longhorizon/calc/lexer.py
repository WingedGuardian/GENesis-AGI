"""Tokenizer for the calc expression engine."""


def tokenize(s):
    """Turn an expression string into a list of (type, value) tokens.

    Token types: 'NUM' (int or float), and the operator/paren characters
    themselves. Whitespace is skipped. Unknown characters raise ValueError.
    """
    tokens = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or c == ".":
            j = i
            while j < n and (s[j].isdigit() or s[j] == "."):
                j += 1
            num = s[i:j]
            tokens.append(("NUM", float(num) if "." in num else int(num)))
            i = j
        elif c in "+-*/()":
            tokens.append((c, c))
            i += 1
        else:
            raise ValueError(f"unexpected character {c!r}")
    return tokens

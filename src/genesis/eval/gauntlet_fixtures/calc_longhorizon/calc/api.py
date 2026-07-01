"""Public API for the calc expression engine.

evaluate(expr) parses and evaluates an arithmetic expression string.

Supported syntax (target behaviour):
  - integer and float literals (e.g. 3, 3.5)
  - binary operators + - * / with standard precedence
    (* and / bind tighter than + and -), left-associative
  - unary minus (e.g. -5, 3 * -2, -(2 + 3))
  - ^ exponentiation: higher precedence than * and /, RIGHT-associative
    (so 2 ^ 3 ^ 2 == 512)
  - parentheses for grouping
  - / is true division (7 / 2 == 3.5)
"""

from calc.evaluator import evaluate_ast
from calc.lexer import tokenize
from calc.parser import Parser


def evaluate(expr):
    return evaluate_ast(Parser(tokenize(expr)).parse())

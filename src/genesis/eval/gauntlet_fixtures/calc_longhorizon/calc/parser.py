"""Recursive-descent parser producing an AST of nested tuples.

AST nodes:
    ('num', value)
    ('binop', op, left, right)
    ('neg', operand)        # unary minus
"""


class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def advance(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self):
        node = self.expr()
        if self.i != len(self.toks):
            raise ValueError("unexpected trailing tokens")
        return node

    def expr(self):
        # NOTE: this treats + - * / all at the same precedence, left to right.
        node = self.atom()
        while self.peek()[0] in ("+", "-", "*", "/"):
            op = self.advance()[0]
            right = self.atom()
            node = ("binop", op, node, right)
        return node

    def atom(self):
        t = self.peek()
        if t[0] == "NUM":
            self.advance()
            return ("num", t[1])
        if t[0] == "(":
            self.advance()
            node = self.expr()
            if self.peek()[0] != ")":
                raise ValueError("expected )")
            self.advance()
            return node
        raise ValueError(f"unexpected token {t}")

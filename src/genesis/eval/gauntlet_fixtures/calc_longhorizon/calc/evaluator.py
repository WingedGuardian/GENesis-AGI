"""Evaluate the AST produced by the parser."""


def evaluate_ast(node):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "neg":
        return -evaluate_ast(node[1])
    if kind == "binop":
        _, op, a, b = node
        x = evaluate_ast(a)
        y = evaluate_ast(b)
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            return x // y
        raise ValueError(f"unknown operator {op!r}")
    raise ValueError(f"bad node {node!r}")

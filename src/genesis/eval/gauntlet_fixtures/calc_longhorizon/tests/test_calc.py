import pytest

from calc import evaluate


# --- precedence ---
def test_prec_add_mul():
    assert evaluate("2+3*4") == 14


def test_prec_mul_add():
    assert evaluate("2*3+4") == 10


def test_prec_chain():
    assert evaluate("2+3*4-1") == 13


# --- associativity (the trap: must stay left-assoc after the precedence fix) ---
def test_sub_left_assoc():
    assert evaluate("10-3-2") == 5


def test_div_left_assoc():
    assert evaluate("100/10/2") == 5.0


# --- parentheses ---
def test_parens():
    assert evaluate("(2+3)*4") == 20


def test_parens2():
    assert evaluate("2*(3+4)") == 14


def test_parens_both():
    assert evaluate("(1+2)*(3+4)") == 21


def test_parens_sub():
    assert evaluate("(2+3)*(4-1)") == 15


# --- unary minus ---
def test_unary_simple():
    assert evaluate("-5") == -5


def test_unary_in_mul():
    assert evaluate("3*-2") == -6


def test_unary_paren():
    assert evaluate("-(2+3)") == -5


# --- true division ---
def test_true_div():
    assert evaluate("7/2") == 3.5


def test_true_div_small():
    assert evaluate("1/4") == 0.25


# --- float literals ---
def test_float_literal():
    assert evaluate("3.5+1.5") == 5.0


# --- exponentiation feature (right-assoc, higher precedence than * /) ---
def test_pow_basic():
    assert evaluate("2^3") == 8


def test_pow_right_assoc():
    assert evaluate("2^3^2") == 512


def test_pow_prec_over_mul():
    assert evaluate("2*3^2") == 18


def test_pow_prec_left_mul():
    assert evaluate("2^2*3") == 12


def test_pow_in_add():
    assert evaluate("1+2^3") == 9

"""Avoid a redundant shlex walk only where default shlex cannot fail."""
import itertools
import shlex
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def parser(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/hooks'))
    return private_module('tokenization_cost_parser', ROOT / 'scripts/hooks/shell_parse.py')


@pytest.mark.parametrize('text', ['', 'plain words', '$(x)', '$((1+2))', 'a\nb', '# note', 'αβ🙂', 'a\x00b'])
def test_quote_free_source_needs_no_extra_shlex_walk(parser, monkeypatch, text):
    def unnecessary_walk(*args, **kwargs):
        raise AssertionError('unnecessary second shlex traversal')
    monkeypatch.setattr(parser.shlex, 'split', unnecessary_walk)
    assert parser._raw_untokenizable(text) is False


def test_raw_tokenization_predicate_matches_default_shlex(parser):
    # Full finite space through length four, plus Unicode/control boundaries.
    alphabet = "a '\"\\\n$"
    texts = [''.join(chars) for size in range(5) for chars in itertools.product(alphabet, repeat=size)]
    texts += ['α🙂\x00', "'α🙂", 'a\\', '"a\\"']
    outcomes = set()
    for text in texts:
        try:
            shlex.split(text)
            expected = False
        except ValueError:
            expected = True
        outcomes.add(expected)
        assert parser._raw_untokenizable(text) is expected
    assert outcomes == {False, True}

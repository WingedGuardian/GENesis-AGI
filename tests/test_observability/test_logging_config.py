"""Tests for Genesis structured logging configuration."""

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from genesis.observability.logging_config import GenesisFormatter, configure_logging


class TestGenesisFormatter:
    def test_format_output(self):
        frozen = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        fmt = GenesisFormatter(clock=lambda: frozen)
        record = logging.LogRecord(
            name="genesis.routing",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="breaker tripped for %s",
            args=("openrouter",),
            exc_info=None,
        )
        output = fmt.format(record)
        assert output == (
            'ts=2026-03-04T12:00:00+00:00 level=WARNING '
            'name=genesis.routing msg="breaker tripped for openrouter"'
        )

    def test_format_with_exc_info(self):
        frozen = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        fmt = GenesisFormatter(clock=lambda: frozen)
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="genesis.test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="something broke",
            args=(),
            exc_info=exc_info,
        )
        output = fmt.format(record)
        assert 'msg="something broke"' in output
        assert "ValueError: test error" in output
        assert "Traceback" in output

    def test_format_with_exc_text(self):
        frozen = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        fmt = GenesisFormatter(clock=lambda: frozen)
        record = logging.LogRecord(
            name="genesis.test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="cached traceback",
            args=(),
            exc_info=None,
        )
        record.exc_text = "ValueError: cached"
        output = fmt.format(record)
        assert "ValueError: cached" in output

    def test_format_without_exc_info_unchanged(self):
        """No traceback appended when exc_info is None."""
        frozen = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        fmt = GenesisFormatter(clock=lambda: frozen)
        record = logging.LogRecord(
            name="genesis.test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="all good",
            args=(),
            exc_info=None,
        )
        output = fmt.format(record)
        assert "\n" not in output
        assert "Traceback" not in output


class TestConfigureLogging:
    def test_sets_up_genesis_hierarchy(self):
        configure_logging(level=logging.DEBUG)
        genesis_logger = logging.getLogger("genesis")
        assert genesis_logger.level == logging.DEBUG
        assert genesis_logger.propagate is False
        assert len(genesis_logger.handlers) >= 1

    def test_idempotent(self):
        configure_logging()
        count = len(logging.getLogger("genesis").handlers)
        configure_logging()
        assert len(logging.getLogger("genesis").handlers) == count

    def test_child_loggers_inherit(self):
        configure_logging(level=logging.WARNING)
        child = logging.getLogger("genesis.routing.breaker")
        assert child.getEffectiveLevel() == logging.WARNING

    def test_no_file_handler_during_tests(self, tmp_path):
        """File handler should be skipped when pytest is in sys.modules."""
        assert "pytest" in sys.modules
        genesis_logger = logging.getLogger("genesis")
        genesis_logger.handlers.clear()
        configure_logging(log_dir=str(tmp_path))
        file_handlers = [
            h for h in genesis_logger.handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert len(file_handlers) == 0

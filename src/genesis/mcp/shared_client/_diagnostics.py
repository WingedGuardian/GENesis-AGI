"""Keep SDK body/parser excerpts out of forwarder diagnostics."""

import logging


class SafeDiagnostics(logging.Filter):
    def filter(self, record):
        return record.name != "mcp" and not record.name.startswith("mcp.")

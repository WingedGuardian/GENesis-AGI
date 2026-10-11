"""Work board — Genesis's front end over a GitHub Projects v2 board.

GitHub owns the board itself (cards, columns, positions, dependencies);
Genesis keeps three small local stores (``db/crud/board.py``) and the glue
that promotes private work onto the board, reconciles it, and reads it back.
The live subsystem map is ``docs/architecture/CURRENT.md`` (section 15).
"""

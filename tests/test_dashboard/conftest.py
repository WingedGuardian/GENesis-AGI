"""Complete the production dashboard blueprint before any fixture registers it.

Some dashboard tests import individual route modules. That populates most of
the shared blueprint but leaves the page route in ``dashboard.api`` unbound.
Flask freezes a blueprint on its first registration, so a later import of the
production entry point otherwise fails depending on test selection/order.
"""

import genesis.dashboard.api  # noqa: F401 — canonical route registration

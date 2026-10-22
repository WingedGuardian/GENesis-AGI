- **A test-vault harness for trying card fill with a test card.**
  `scripts/payments_test_vault.py` builds a separate vault that holds only
  Stripe's public test card (labelled `TEST 4242`), serves it on its own socket,
  and asks for each approval in its own terminal (type `y`) instead of on
  Telegram. A branch's card-fill tool can then be run live without the real
  vault, its socket, its cards or the bot. Every approval it shows starts with
  a `TEST:` line. See
  `docs/reference/payment-test-vault.md`.

- **A test-vault harness for trying card fill with a test card.**
  `scripts/payments_test_vault.py` builds a separate vault that holds only
  Stripe's public test card (labelled `TEST 4242`), serves it on its own socket,
  and sends its approval taps through a second, test-only Telegram bot
  (`PAYMENT_TEST_VAULT_BOT_TOKEN` in `secrets.env`), so a branch's card-fill
  tool can be run live without the real vault, its socket, its cards or the
  main bot. Every tap it sends starts with a `TEST:` line. See
  `docs/reference/payment-test-vault.md`.

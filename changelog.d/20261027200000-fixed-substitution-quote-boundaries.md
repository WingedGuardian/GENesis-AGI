- **Keep guarded commands visible across quoted shell substitutions.** Independent
  substitution quote contexts prevent inner quotes from hiding later operations,
  and escaped literal openers no longer masquerade as executable substitutions.
  Ambiguously decoded scripts and substitutions without a proven closing boundary
  are refused instead of trusted, including fragmented arithmetic expressions.

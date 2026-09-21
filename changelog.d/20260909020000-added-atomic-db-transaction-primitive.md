- **Multi-statement database writes can now be made all-or-nothing.** Genesis
  shares a single database connection across everything running at once, and
  until now each statement was individually protected while a sequence of them
  was not — so two writes that only make sense together could be split apart by
  another task committing in between, leaving the first applied and the second
  not. A new transaction block holds the connection for the whole sequence and
  either applies all of it or none of it. Statements that would end the
  transaction early are refused inside the block, on every path that reaches the
  database — including the lower-level cursor that ordinary queries hand back,
  which is the one that made the earlier guards incomplete.

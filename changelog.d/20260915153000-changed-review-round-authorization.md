## Changed

- Review continuation now counts distinct GitHub-reviewed commit heads. Ordinary pull requests require a fresh native user approval for every review request and fix commit after four reviewed heads, with round six and later strongly discouraged. Review-gate changes use the expedited two-round lane plus one exact-head confirmation. Autonomous sessions deny actions that need approval, and unreadable evidence never becomes a zero-round allowance.

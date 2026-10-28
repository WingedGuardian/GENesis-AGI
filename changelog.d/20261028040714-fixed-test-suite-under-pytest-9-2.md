- **The test suite passes again under pytest 9.2.** Two tests failed on any
  install, or CI run, that picked up pytest 9.2.0. One of them built its
  fixture on a private pytest attribute that 9.2 removed. The other read the
  first line of a run's output, which pytest 9.2 now opens with a
  pytest-asyncio deprecation warning. The fixture now uses a public pytest hook,
  and `pyproject.toml` sets `asyncio_default_fixture_loop_scope = "function"`,
  which silences the warning without changing which event loop any existing
  async fixture runs on.

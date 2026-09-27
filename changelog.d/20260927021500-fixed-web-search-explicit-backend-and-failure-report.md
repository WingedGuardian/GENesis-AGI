- **`web_search` now runs the backend you ask for, and says exactly why a search
  failed.** `backend="brave"` used to run the whole SearXNG-then-Brave chain, so
  it was answered by SearXNG whenever that was up. `backend="brave"` and
  `backend="searxng"` now run only the named backend. A search where every
  backend failed used to report `backend_used: "searxng"` and a bare "All search
  backends unavailable". It now reports `backend_used: null`, and the error
  names each backend it tried with its reason, e.g.
  `searxng: ConnectError: …; brave: API_KEY_BRAVE is not set`. That makes an
  unset Brave key or a missing SearXNG service visible at a glance. Voice web
  search now uses the standard chain (TinyFish first). It used to ask for Brave
  alone, which skipped TinyFish. A failed voice search is now reported as a
  failure, not as "No results found".

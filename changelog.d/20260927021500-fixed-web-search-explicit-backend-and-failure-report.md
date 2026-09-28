- **`web_search` now runs the backend you ask for, and says exactly why a search
  failed.** `backend="brave"` used to run the whole SearXNG-then-Brave chain, so
  it was answered by SearXNG whenever that was up. `backend="brave"` and
  `backend="searxng"` now run only the named backend. A search where every
  backend failed used to report `backend_used: "searxng"` and a bare "All search
  backends unavailable". It now reports `backend_used: null`, and the error
  names each backend it tried with its reason, e.g.
  `searxng: ConnectError: …; brave: API_KEY_BRAVE is not set`. That makes an
  unset Brave key or a missing SearXNG service visible at a glance. Voice web
  search now uses the standard chain (TinyFish first). It used to ask for
  `brave`, which in practice meant SearXNG first, and a SearXNG whose upstream
  engines are rate-limited can answer spoken questions with unrelated pages
  (a weather question returned a software download page). A failed voice search
  is now reported as a failure, not as "No results found", and voice search
  gives up after 20 seconds with a "timed out" error rather than outlasting the
  30-second voice tool-call limit. TinyFish search snippets now carry the same
  untrusted-content markers as SearXNG and Brave snippets. A failed explicit search whose credential is missing now says so (for example
  `API_KEY_TAVILY is not set`); other failures return a short summary, with the detail
  in the server log.

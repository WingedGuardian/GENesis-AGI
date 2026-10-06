- Add `python -m genesis.eval.qualification`, which measures whether an
  OpenRouter routing alias agrees with owner-labelled references on the judge
  rubrics, J9 relevance and procedure novelty, and pairs two aliases side by side.
  Each request is pinned to one upstream with a price ceiling, spend is bounded by
  a dedicated credit-limited key and a request cap, and paid answers are kept so a
  rerun never pays twice. Production model selection is unchanged.

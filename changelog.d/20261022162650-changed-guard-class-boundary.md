- **The rule against hand-rolled parsing in guards now covers the whole class.**
  The rule now covers any check that predicts a future event from text, not
  only git argv. A guard may allow or exempt only on a fact it has resolved.
  When it cannot resolve that fact before the event, it fails closed, abstains,
  or checks after the event; it never lets a pattern stand in for the fact when
  allowing.

# Interpreting retained inbox evidence

`scripts/inbox_coverage_report.py` reports selected note/URL pairs and whole
logical items separately. URL totals deduplicate tracking-normalized URLs per
note; logical-item totals preserve repeated occurrences, annotations and entire
text blocks. Empty/text-only notes remain in the selected-note count. Bracketed
blocks are labelled for review; report generation executes no directives.
Original item text remains intact even when its URL summary strips tracking
parameters.
Exact item association uses the queue's whole-item identity, preserving interior
indentation. Baseline-only text or URL history creates uncertainty, never proof
that an item was dispatched. Unknown input remains uncertain across every status.
Cumulative baselines are sorted sets of stripped lines. Text-history uncertainty
uses those lines without reconstructing item boundaries or their original order.

## Evidence boundaries

- Valid versioned input uses the runtime's strict decoder. A corrupt array is
  rejected whole; valid elements are not salvaged into positive associations.
- Legacy plain text retains dispatched URL membership. Its erased multi-line
  item boundaries cannot establish an exact logical-item match. The runtime's
  existing `stored_item_texts` helper supplies only unambiguous boundaries.
- Cumulative `evaluated_content` is baseline history, not dispatched-input or
  source-acquisition proof. A `baseline_only` association stays distinct.
- Each historical row keeps its status, input format and response reference.
  Failed/superseded rows and missing/corrupt input are not silently completed.
  Non-text storage is labelled unreadable, and baseline-only or historical-only
  membership remains unattributable rather than being called never encountered.
  History outside selected paths is counted, not guessed to be a rename.
- Response presence, parsed `Source` coverage and incidental URL mentions are
  separate. Without a parsed Source field the label is `source_not_recorded`,
  even if the response mentions the URL. No date-based contract era is inferred.
- The selected completed row supplies its own coverage evidence; responses
  from different rows are never pooled to prove coverage. A URL match alone
  does not prove that a changed annotation was evaluated.

The existing URL identity/coverage policy is unchanged. `never` means no
retained matching input, not that nothing ever happened. Unattributable history
remains uncertainty. None of these fields proves acquisition, literal quote
fidelity or sound interpretation. Unreferenced response files are not
inventoried, so report absence is not proof that no such artifact exists.

Require a successful fresh invocation and reconcile all item denominators.
Read complete source notes and evaluation responses for owner review; a report
row is not a substitute for their content. The report changes no evaluation,
baseline, approval, retry, memory or provider state.

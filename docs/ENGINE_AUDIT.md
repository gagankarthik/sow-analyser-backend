# Core engine audit & changes

A review of the extraction, classification, embedding, vector/similarity, diff,
timeline, and persist engines. This records the fixes applied in this pass and a
prioritised list of follow-ups. The deeper items need a deploy + a real document
run to verify, since the pipeline depends on Lambda, Step Functions, OpenSearch,
and DynamoDB.

## Fixes applied — pass 2 (extraction correctness + amendment replay)

1. **Amendment diff no longer fabricates deletions** (`stages/diff.py`)
   An amendment is a DELTA document — `classify` only extracts the clauses the
   amendment introduces, not the whole parent. The old `_diff` compared the
   amendment's handful of clauses against the *full* parent and emitted a
   `body→""` deletion for every parent clause the amendment left untouched, plus
   a brand-new "addition" for every amendment clause (numbering never lines up).
   The timeline stage then popped all those clauses, collapsing `currentState` to
   the amendment's few clauses. Fixed: for AMENDMENT docs the diff is now driven
   by `classification.amendment.changes[]` (changeType / category / targetSection
   / before→after), mapping each change onto the parent clause it targets (by
   number, then title similarity). Untouched clauses are preserved; only a
   matched `deletion` removes state. The full-clause path remains for genuine
   re-uploads, now guarded so a sparse/half-failed extraction can't wipe the
   contract.

2. **Plain-text (.txt) extraction implemented** (`stages/parse.py`,
   `shared/schema.py`, `api/handler.py`)
   The upload API advertised `.txt` (and `.doc`) but `parse._detect_type` only
   handled pdf/docx and raised `ValueError` on anything else — so every `.txt`
   upload crashed the pipeline. Added a `_parse_txt` extractor (utf-8 with a
   latin-1 fallback) and a `TEXT` extraction method; type detection now prefers
   magic bytes over extension. Dropped legacy binary `.doc` from the API
   allowlist since there is no parser for it in the layer.

3. **Impact-score LLM call no longer 400s** (`stages/diff.py`)
   `_IMPACT_SCHEMA` used `minimum`/`maximum`, which OpenAI strict Structured
   Outputs rejects — every refinement call failed (caught as a warning), so
   scores silently stayed at the heuristic value. Removed the unsupported
   keywords; the [1,100] bound is enforced in the prompt and clamped in code.

4. **Executed amendments count toward current state** (`stages/timeline.py`)
   `currentState` only applied amendments whose lifecycle was exactly `active`,
   so a `signed` (executed) amendment was treated as pending. Now any executed
   lifecycle (`signed`/`active`/`renewal`/`expired`) is in force; only true
   pending states (`draft`/`review`/`negotiation`/`approval`) stay in
   `futureState`.

5. **Parent-match threshold lowered to match the weight scheme**
   (`shared/config.py`)
   With weights reference 0.25 / hybrid 0.45 / structural 0.18 / title 0.12, the
   0.7 floor rejected legitimate parents (a named, title-matching parent with
   good hybrid recall but a different clause structure scores ~0.59). Default is
   now 0.5 (override via `PARENT_MATCH_MIN_CONFIDENCE`).

6. **Unit tests added** (`tests/`)
   26 focused tests covering parse type detection + txt decode, the amendment vs
   re-version diff (incl. the no-phantom-deletion regression), diff→timeline
   replay reconstruction, risk aggregation, hybrid-search degenerate-span
   normalisation, and the classify validation write-back. Run with
   `tests/requirements.txt` (see header).

## Fixes applied — pass 1

1. **Parent matching now uses the explicit parent reference**
   (`lambdas/pipeline/stages/graph.py`)
   Amendments that name their parent ("pursuant to SOW-2024-001") previously
   relied only on fuzzy clause similarity + structural hash + title. We now
   search for the stated `identification.parentReference` directly and fold a
   reference↔title comparison into the score. Signal weights rebalanced to
   reference 0.25 / hybrid 0.45 / structural 0.18 / title 0.12. This makes
   original-vs-amendment linkage far more reliable, which is what drives the
   timeline and contract-value rollup.

2. **Hybrid search no longer drops a lone candidate**
   (`lambdas/shared/opensearch.py`, `hybrid_search._norm`)
   Min-max normalisation used `span = (hi - lo) or 1.0`, so when a channel had a
   single hit (or all-equal scores) every result normalised to **0.0** and was
   effectively discarded. Degenerate spans now map to a tie at the top (1.0).
   This matters for parent matching when few SOW/MSA candidates exist.

3. **Embedding dimension guard**
   (`lambdas/pipeline/stages/embed.py` + `VECTOR_DIM` in `opensearch.py`)
   If `EMBEDDING_MODEL` is ever changed to a different-dimension model
   (e.g. `text-embedding-3-large` = 3072) without updating the index, every
   `index_clause_vector` call would fail as a *non-fatal* per-clause warning and
   the document would be silently unsearchable. The embed stage now fails fast
   with an actionable error, and the index dimension is a single exported
   constant (`VECTOR_DIM = 1536`).

## Note on the chat model

`gpt-4.1-mini` is a valid OpenAI model (GPT-4.1 family) and is the intended chat
model — it is **not** a misconfiguration. No change made there.

## Recommended follow-ups (verify on a deploy)

Prioritised, with the engine each touches. These were identified by review but
not changed here because they benefit from running against real documents.

| Pri | Engine | Area | Recommendation |
|-----|--------|------|----------------|
| High | diff | impact scoring | Weight financial deltas explicitly (parse $ before/after); a Fees change $10k→$50k should outrank a 1-char edit. Bound the LLM score to [1,100]. |
| High | classify | validation pass | Retry the financial-reconciliation call on failure instead of returning a low-confidence stub; persist `confidence`/`validation` to the doc META so the UI can flag low-confidence extractions. |
| Med | timeline | ordering | Sort amendments by `(effectiveDate, createdAt)` for a deterministic tiebreaker; track deletes as flagged state rather than popping, so a later amendment can re-modify. |
| Med | parse | extraction | Make the Textract-fallback heuristic structural (lines/page), not just a 200-char total; add retry/backoff around the Textract poll. |
| Med | persist | atomicity | Write META + VERSION via `TransactWriteItems` so a partial failure can't mark a doc READY with no version. |
| Low | openai_client | latency | Lower the HTTP timeout (120s → ~30s) and cap retries so a stuck call can't consume most of the Lambda budget. |

## Performance opportunities

- Embed and graph stages are independent of each other's writes and could run in
  parallel in the Step Functions definition.
- Cache parent `classification.json` per execution in the timeline/diff stages
  instead of re-reading from S3 per version.

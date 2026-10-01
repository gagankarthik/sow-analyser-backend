# Core engine audit & changes

A review of the extraction, classification, embedding, vector/similarity, diff,
timeline, and persist engines. This records the fixes applied in this pass and a
prioritised list of follow-ups. The deeper items need a deploy + a real document
run to verify, since the pipeline depends on Lambda, Step Functions, OpenSearch,
and DynamoDB.

## Pass 3 — extraction completeness, dates, clause types, retrieval, speed

What changed in the engine, and what it now guarantees. Verified offline with faked model replies; real
extraction ACCURACY still needs real documents (see the last section).

### How a document is analysed now

1. **Parse** (`stages/parse.py`, `shared/docx_text.py`) — full text, no page or character cap. DOCX is read
   from the XML directly: tables in place, content controls, tracked insertions, text boxes, automatic
   numbering, headers/footers, footnotes. PDF: per page, two-column pages read column by column, running
   headers/footers removed after their first occurrence, scanned or rotated pages inside a text PDF sent to
   OCR. What could not be read is reported in `parsed.stats.warnings`, not dropped.
2. **Segment** (`shared/segment.py`, code — no model) — the text is cut into clauses: preamble, numbered
   clauses and sub-clauses, schedules / exhibits / annexes, signature block. Every word lands in a clause.
   Coverage is measured (`extraction.coverageRatio`); if it is below `MIN_COVERAGE_RATIO` the stage falls
   back to paragraph blocks and flags the document.
3. **Label** (model, bounded parallel batches) — each clause gets title / type / risk / summary by id. The
   reply is reconciled against the ids sent; missing or failed ones are retried in smaller batches; what
   still has no label is kept as `classificationStatus: "unclassified"`, `needsReview: true`.
4. **Extract** (model) — document-level facts. A document longer than `CLASSIFY_MAX_INPUT_TOKENS` is read
   in overlapping windows and merged. Nothing is truncated.
5. **Validate** (model + code) — money re-read with source quotes, then checked in code: the quote must
   exist in the document and state the amount (a dropped "million" is corrected from the quote), parts
   must add up, an amendment's delta gets its sign from the arithmetic or the wording, every figure is
   tied to the clause it came from.
6. **Derive** (code) — key dates (`shared/keydates.py`), clause types (`shared/clause_types.py`),
   playbook outcomes per clause, compliance, pillar.

### Where information used to be lost

| Where | What happened | Now |
|---|---|---|
| `classify.py` head+tail truncation at 30k tokens | the middle of a long contract was never seen | windows, merged |
| `classify.py` verbatim clause bodies in the model's output | a long contract did not fit the output budget; the run failed or clauses were cut | bodies come from the document, not the model |
| `classify.py` model-chosen clause list | the model could skip, merge or paraphrase clauses | code segmentation + coverage check |
| `parse.py` DOCX via `doc.paragraphs` + `doc.tables` | tables moved to the end; content controls, tracked insertions, text boxes, numbering, headers, footnotes skipped | direct XML walk |
| `parse.py` PDF: only a fully empty PDF went to OCR | scanned pages inside a text PDF were dropped | per-page OCR merge |
| `parse.py` TXT latin-1 fallback | UTF-16 files became NUL-riddled; € and curly quotes lost | BOM / UTF-16 / cp1252 |
| `parse.py` Textract `PARTIAL_SUCCESS` treated as failure | readable pages discarded | kept, with a warning |
| `embed.py` one embedding per whole clause | a clause over the model's input limit failed the batch | chunked with overlap |
| `opensearch.py` k-NN filtered after ranking | a doc-scoped question could return nothing | exact filtered search when scoped |
| `opensearch.py` BM25 only if k-NN returned < k | exact-term matches crowded out | reciprocal-rank fusion |
| `rag/handler.py` 1,200-character clause cap | the end of every longer clause never reached the model | whole chunks, merged per clause |
| `timeline.py` state keyed by clause number | two clauses with the same number overwrote each other | numbers made unique |
| `timeline.py` replayed every change row | an amendment analysed twice was applied twice | latest version only |
| `diff.py` target `"Section 4.2"` compared as a whole phrase | never matched clause `4.2` | number extracted |
| `diff.py` short amendment with no itemised changes | invented "clause 1 changed" by number collision | reports nothing rather than something false |
| `persist.py` unrated clause counted as `low`; `overallRisk: "low"` with nothing rated | a risk rating nobody gave | `riskCounts.unrated`, null when unknown |
| `persist.py` replaced the whole document row | undid owner / project changes made during the run | field update, READY written last |
| `openai_client.py` SDK retries × our retries, 120 s timeout | up to 12 attempts; one hung call could eat the Lambda | one retry loop, per-request timeout, stage deadline |
| `openai_client.py` invalid JSON / refusal not handled | crash with no retry | retried, then a typed error |
| logs: `error=str(exc)`, `log.exception` | exception text can quote the document | exception TYPE and code location only |

### Left as is (and why)

* **PDF tables** are extracted as text lines (cell text is present, column alignment is not). Emitting
  `extract_tables()` as well would double every figure in a fee table.
* **Review comments** in DOCX are not read (they are not contract text). Tracked DELETIONS are excluded.
* **Lettered top-level headings** ("A. Scope") are not treated as clause headings — too easily confused
  with initials and list items; such documents use the ALL-CAPS / paragraph path.
* **The k-NN index engine** (`nmslib`) cannot filter before ranking. Document- and project-scoped searches
  use an exact filtered query instead; tenant-wide search over-fetches. Moving the index to an engine with
  efficient filtering needs a new index and a re-analysis of all documents.
* **An amendment uploaded before its parent** is marked `lineageStatus: "unmatched"`; it is not re-linked
  automatically when the parent arrives — re-analyze the amendment.

### Not verifiable offline

* How accurately a real model fills the schemas on real contracts (dates, money, clause types, risk).
* That the provider accepts the larger structured-output schema, and the request forms used for models
  that reject `max_tokens` / temperature / `json_schema`.
* Heading detection, two-column detection and header/footer removal on real PDFs (tested on synthetic pages).
* The exact (`script_score`) k-NN query and `put_mapping` against the live OpenSearch domain.
* X-Ray trace propagation into worker threads; real latency and rate-limit behaviour.

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

Status after pass 3 (the original list is kept for the record):

| Pri | Engine | Area | Recommendation | Status |
|-----|--------|------|----------------|--------|
| High | diff | impact scoring | Weight financial deltas explicitly (parse $ before/after); a Fees change $10k→$50k should outrank a 1-char edit. Bound the LLM score to [1,100]. | Open (score is bounded; the signed value delta now travels with the diff). |
| High | classify | validation pass | Retry the financial-reconciliation call on failure instead of returning a low-confidence stub; persist `confidence`/`validation` to the doc META so the UI can flag low-confidence extractions. | Done — the call is retried by the client; a failure sets `needsReview`; `extractionConfidence`, `needsReview`, `reviewReasons` are on the record. |
| Med | timeline | ordering | Sort amendments by `(effectiveDate, createdAt)` for a deterministic tiebreaker; track deletes as flagged state rather than popping, so a later amendment can re-modify. | Ordering done; delete-tracking open. |
| Med | parse | extraction | Make the Textract-fallback heuristic structural (lines/page), not just a 200-char total; add retry/backoff around the Textract poll. | Done per page (scanned / rotated pages inside a text PDF go to OCR). |
| Med | persist | atomicity | Write META + VERSION via `TransactWriteItems` so a partial failure can't mark a doc READY with no version. | Done differently: version and change rows are written first, the document becomes READY last. |
| Low | openai_client | latency | Lower the HTTP timeout (120s → ~30s) and cap retries so a stuck call can't consume most of the Lambda budget. | Done — per-request timeout, one retry loop, and a stage deadline no request may outlive. |

## Performance opportunities

- Embed and graph stages are independent of each other's writes and could run in
  parallel in the Step Functions definition.
- Cache parent `classification.json` per execution in the timeline/diff stages
  instead of re-reading from S3 per version.

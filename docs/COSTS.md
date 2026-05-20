# Cost projection

> All figures in USD, on-demand pricing, us-east-1, as of 2026.

## Volumes assumed

| Tier | Docs/month | Avg pages | Clauses/doc | Storage (raw + processed) |
|---|---|---|---|---|
| Small | 1,000 | 18 | 25 | 50 GB |
| Medium | 10,000 | 22 | 30 | 500 GB |
| Large | 100,000 | 22 | 30 | 5 TB |
| XL | 1,000,000 | 22 | 30 | 50 TB |

## Per-service breakdown

### S3
- $0.023/GB-month + $0.0004 per 1k GET + $0.005 per 1k PUT
- Small: ~$2 | Medium: ~$12 | Large: ~$120 | XL: ~$1,200

### Lambda
- ~7 invocations per doc × 1s avg × 1024 MB
- Small: <$1 | Medium: ~$5 | Large: ~$50 | XL: ~$500

### Step Functions Express
- $1.00 per million state transitions
- ~7 transitions/doc
- Small: <$1 | Medium: ~$1 | Large: ~$10 | XL: ~$100

### Textract
- $0.0015/page (Detect) or $0.05/page (Analyze with tables/forms)
- Assume 30% need Detect, 5% need Analyze
- Small (18k pages OCR'd): $7 | Medium: $90 | Large: $900 | XL: $9,000

### DynamoDB (on-demand)
- $1.25/M writes + $0.25/M reads + $0.25/GB-month storage
- Assume 50 writes / 200 reads per doc lifecycle
- Small: ~$3 | Medium: ~$25 | Large: ~$250 | XL: ~$2,500

### OpenSearch
| Tier | Cluster | Monthly |
|---|---|---|
| Dev | 3× t3.small.search | $80 |
| Small prod | 3× t3.medium.search | $180 |
| Medium prod | 3× m6g.large.search | $280 |
| Large prod | 3× r6g.xlarge.search + 3× UltraWarm | $1,100 |
| XL prod | 6× r6g.2xlarge.search + cold storage | $4,500 |

### OpenAI
- Embeddings (text-embedding-3-small): $0.02 / 1M tokens
- Chat (gpt-4o-mini classify): $0.15 in / $0.60 out per 1M tokens
- RAG queries: ~10/doc lifecycle × 1k context = 10k tokens
- Small: $5 | Medium: $40 | Large: $400 | XL: $4,000

### AppSync
- $4 per million query + $2 per million subscription message + $0.08/hr connection
- Assume 10 queries + 20 subscription messages per doc + 100 active connections
- Small: $3 | Medium: $15 | Large: $150 | XL: $1,500

### Networking, CloudWatch, Secrets, KMS, EventBridge
- Roughly 10% of total

## Total

| Tier | Without Neptune | With Neptune (db.t3.medium baseline) |
|---|---|---|
| **Small** | ~$110/mo | ~$460 |
| **Medium** | ~$270/mo | ~$620 |
| **Large** | ~$1,900/mo | ~$2,300 |
| **XL** | ~$19,200/mo | ~$20,200 |

## Cost-down levers (high to low impact)

1. **Skip Neptune** in v1 — saves $350+/mo flat, plus $0.10/hr per replica.
2. **OpenAI cache** — cache embeddings on identical clause text (hash-based). Cuts embedding cost 60–80% in steady state where amendments share boilerplate.
3. **Textract gating** — only call Textract when pdfplumber returns < 100 chars/page. Saves 50–70% of OCR cost.
4. **OpenSearch UltraWarm tier** — move docs older than 90 days to UltraWarm. Saves 50% on long-tail vectors.
5. **Embedding model: small vs large** — `text-embedding-3-small` is 5× cheaper than `large` and retrieval quality is within 1-2 percentage points for legal text.
6. **gpt-4.1-mini vs gpt-4o for classify** — 15× cheaper, accuracy difference < 2% on structured classification.
7. **DynamoDB on-demand vs provisioned** — at large scale, provisioned with auto-scaling can be 40% cheaper.
8. **Compress audit blobs** — `zstd` reduces storage 70%+, S3 PUT cost is per-object not per-byte.

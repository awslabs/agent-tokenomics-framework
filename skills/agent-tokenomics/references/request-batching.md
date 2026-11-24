# 6. Request batching

**Pillar:** Optimization (orchestrate native) · **Quality risk:** None — same model, same prompt; the trade-off is latency (hours, not seconds).

## Problem

Non-interactive bulk workloads (nightly classification, document enrichment, eval
runs, backfills, report generation) are often run through the on-demand API in a loop,
paying the full on-demand price and fighting throttling limits.

## Do

1. **Identify offline/async paths** — no user waiting on the response.
2. **Route them to Amazon Bedrock batch inference** (`CreateModelInvocationJob`):
   JSONL input in S3 → job → JSONL output in S3. Batch is priced at a discount to
   on-demand for supported models (commonly ~50%; check current pricing per model).
3. **Keep prompts cache-friendly anyway** — the same stable-prefix rules apply.
4. **Make jobs idempotent and resumable** — record `recordId` so partial failures can
   be re-submitted, not re-run.

## Don't

- Don't batch live customer turns or anything inside an agent's interactive loop.
- Don't loop `invoke_model` / `converse` over thousands of records in a Lambda or script.

## Code smells to look for

- `for record in records: client.converse(...)` in a scheduled job, ETL, or eval script.
- Retry/backoff logic written mainly to survive throttling on bulk jobs.
- Cron/EventBridge-scheduled functions calling Bedrock on-demand at scale.

## Verify

- Cost per 1k records on-demand vs batch for the same model and prompt.
- Job completion time fits the business SLA.

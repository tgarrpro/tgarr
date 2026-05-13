# Pricing

tgarr ships in two pieces:

1. **tgarr** — the self-hosted app (this repo, MIT-licensed, free forever)
2. **registry.tgarr.me** — the hosted federation service

The app is and remains free open source. The hosted service is what we charge for.

## Tiers

All paid tiers are **annual** subscriptions, matching the NZB-indexer convention (NZBGeek, DrunkenSlug, NZB.su all charge yearly).

| | **Free** | **Standard** | **Plus (NSFW)** |
|---|---|---|---|
| **Price** | $0 | **$9.99 / year** | **$29.99 / year** |
| Per-month equivalent | — | $0.83 / mo | $2.50 / mo |
| API key | not required | required | required |
| Registry pull | verified SFW only (≥3 contributors) | full SFW registry | full SFW + full NSFW |
| Rate limit | 100 queries / day | 10,000 / day | unlimited |
| Federated release search | ❌ | ✅ | ✅ |
| Bulk export | ❌ | ❌ | ✅ |
| Web search UI | ❌ | ✅ | ✅ |
| Email support | community Discord | priority | priority + adult-content compliance docs |
| Age verification | n/a | n/a | required at checkout |

## Why yearly only

1. **Market convention.** Every NZB indexer the *arr ecosystem already pays — NZBGeek, NZB.su, DrunkenSlug, OZnzb — charges yearly. Users are conditioned for it.
2. **Lower churn.** Monthly subscribers cancel in months 2–3; yearly subscribers stay a year by default.
3. **Lower PSP fees.** Stripe takes ~2.9% + 30¢ per transaction. Yearly = 1 charge/year vs 12 charges/year.
4. **Simplified compliance.** VAT MOSS / 2257 records / age-verification are annual checks rather than monthly.
5. **Pre-paid cash flow.** A new subscriber funds a year of infrastructure on day one.

## Why $9.99 specifically

Loss-leader pricing to enter the indexer market. NZBGeek is $20+ historical; pricing at half undercuts the established players and removes purchase friction. Pricing is grandfathered for early users when the headline price moves to $14.99 or $19.99 later.

## Why $29.99 for Plus

- 3× base = standard adult-tier multiplier across SaaS.
- Adult-friendly payment processors (Verotel, CCBill, Segpay) take noticeably higher rates than Stripe (often 6–14% + per-transaction fees); the multiplier covers it.
- $29.99 / year is still ~$2.50/mo equivalent — below every OnlyFans or Pornhub Premium tier.

## Lifetime tier (proposed for launch)

`Founders Lifetime` — $99 one-time, limited to first 1,000 buyers. Includes Standard tier forever. Removed from purchase flow when cap is hit. Generates cold-start cash to fund infrastructure + legal setup.

## What the free tier intentionally does not include

- Direct NSFW access (legal compliance overhead is real).
- Federated release search (the unique-value service that justifies a paid tier).
- API access for tooling integrations.
- The full deduplicated channel list (only `verified` channels — those with ≥3 distinct community contributors — are exposed).

A free tier exists so anyone can verify the self-hosted app works against the registry without payment. Paid tiers unlock the curated, deduplicated, and search-able layer that takes meaningful infrastructure to maintain.

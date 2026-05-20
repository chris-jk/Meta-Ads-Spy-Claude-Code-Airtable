# Future: hosted competitor ad spy as a website upsell

Parking-lot doc — not a plan to execute now. Revisit after we've sold enough
websites that an upsell flow makes sense.

## Pitch

We already build websites for entrepreneurs. After a site ships, the next
question is always *"how do I get traffic?"* — and the first honest answer is
*"look at what your competitors are running and steal what works."*

This repo already does that, as a local CLI. The upsell is: customer logs in,
types in their competitors (or we auto-suggest from their site copy), and they
get a live Airtable / dashboard of competitor ads — copy, creative, days
running. They never touch a terminal.

## What we already have (free, working today)

- Local CLI: `pull_ads.py` → Selenium UI scraper → Airtable
- Works for any country (US implementation in `lib/ui_scraper.py`)
- Pulls full body copy, headline, landing, creative file, start date, days
  running (the "is this ad converting" signal)
- Costs $0 to run on your laptop

## What "hosted service" would mean

Customer can't run Selenium themselves. We need to host the browser fleet and
expose a UI / API. Cloudflare Workers can't run a browser, so the architecture
is:

```
[customer browser] → Cloudflare Pages (UI) → CF Workers (API)
                                                  ↓
                                           Job queue (CF Queues or DB)
                                                  ↓
                                     Scraper workers (Fly.io / VPS / Browserless)
                                                  ↓
                                        Postgres (Supabase) + R2 (creatives)
                                                  ↓
                                Either push to customer's Airtable OR our own UI
```

## Realistic monthly budgets

| Tier | $/mo | What it gets you |
|---|---|---|
| Lean MVP | $5–10 | Hetzner CX22 + free CF Pages/Workers + free Supabase. Handles ~50–100 customers running weekly scrapes. |
| Comfortable | $30–60 | Fly.io autoscale workers, Supabase Pro, R2 creative storage. Handles a few hundred customers. |
| No-Chrome-babysitting | $50–100 | Same as above but using Browserless.io instead of self-hosted Chrome ($50/mo for 5k sessions). Trade money for less ops. |

Add **$50–100/mo residential proxies** once Meta starts IP-banning you (will
happen). And **a domain** (~$12/yr).

Marginal cost per customer is effectively zero until volume gets serious —
compute isn't the bottleneck, Meta rate-limiting is.

## Pricing thoughts (back-of-envelope)

If we charge $20/mo per customer for "weekly scrape of 10 competitors":
- 50 customers = $1k MRR, ~$50/mo costs → ~$950 margin
- Customer perceived value: a manual VA doing this = $200+/mo, paid tools
  (AdSpy, PowerAdSpy, Foreplay) = $50–150/mo

Doesn't need to be a big business to be worth running.

## Decision points to revisit

1. **Bundle vs upsell?** Free with website (sticky retention) or paid add-on
   (revenue)?
2. **Customer's Airtable or our UI?** Airtable = zero frontend work, easy
   handoff. Our UI = better UX, more lock-in, more dev cost.
3. **Self-host Chrome or pay Browserless?** $5 VPS vs $50/mo — depends how
   much ops time we want to spend.
4. **Who owns the data?** If we host the DB, we can do interesting things
   (trend reports, "ads winning right now in your niche", etc.). If it lives
   in the customer's Airtable, we can't.

## Hidden costs people miss

- Meta changes selectors every few months → ongoing maintenance, not money
- Meta IP-bans long-running scrapers → residential proxies eventually mandatory
- Customer support / Airtable hand-holding eats more time than the code does

## Next steps (when we come back to this)

1. Get 10+ paying website customers asking "now what about ads?" before
   building anything. The signal validates the upsell exists.
2. Manual fulfillment first — run the existing CLI for them, dump results in
   *their* Airtable. Costs us $0, proves the value, sets the price.
3. Only then automate.

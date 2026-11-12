# edge-lab-odds

Odds pipeline for **Edge Lab**, a private play-money NBA/NFL simulator. Runs entirely inside Supabase (project: Naura CRM, schema `edge_lab`).

**Schedule:** pg_cron calls the `edge-lab-odds` Edge Function at 16:00 and 22:30 UTC (11:00am and 5:30pm ET in winter).

Each run:
1. Pulls NBA + NFL odds (moneyline, spread, total) from The Odds API → `edge_lab.odds_snapshots` (history) and `edge_lab.latest_odds`.
2. **Quant bot** picks every game starting within 7 hours → `edge_lab.bot_picks`: the side with the best EV vs. the consensus no-vig line at the best available price; near-ties follow the line move since open.
3. When started games lack results, pulls final scores → `edge_lab.game_results`, grades Quant picks and records closing-line value.

The research bots (Scout, Analyst, Consensus) run as Claude scheduled tasks that read these tables and write picks into the Edge Lab app.

## Files
- `supabase/migrations/001_edge_lab_schema.sql`: tables (RLS on; schema not exposed to the public API)
- `supabase/functions/edge-lab-odds/index.ts`: HTTP entry; checks the Vault job token
- `supabase/functions/edge-lab-odds/job.js`: odds, Quant picks, grading, CLV (plain JS; tested in Node)

## Secrets (Supabase Vault)
- `edge_lab_odds_api_key`: The Odds API key
- `edge_lab_job_token`: token pg_cron sends in `x-job-token`

## Credits
Free Odds API plan = 500/month. Odds = 3 markets × 1 region per league = 6 per run; scores = 2 per league, only when games are waiting (~450–480/month).

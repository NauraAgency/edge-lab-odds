-- Edge Lab data lives in its own schema, walled off from the rest of the project.
create schema if not exists edge_lab;
revoke all on schema edge_lab from anon, authenticated;

create table if not exists edge_lab.odds_snapshots (
  id bigserial primary key, fetched_at timestamptz not null, league text not null, event_id text not null,
  commence_time timestamptz, home_team text, away_team text, book text not null, market text not null,
  outcome text not null, price integer, point numeric);
create index if not exists odds_snapshots_event on edge_lab.odds_snapshots(event_id, fetched_at);

create table if not exists edge_lab.latest_odds (
  event_id text primary key, league text, commence_time timestamptz, home_team text, away_team text,
  fetched_at timestamptz, books jsonb);

create table if not exists edge_lab.bot_picks (
  id bigserial primary key, bot text not null, event_id text not null, league text, market text not null,
  side text not null, point numeric, price integer, book text, fair_prob numeric, ev numeric,
  picked_at timestamptz, commence_time timestamptz, home_team text, away_team text,
  status text default 'open', profit_units numeric, home_score int, away_score int,
  close_fair numeric, clv numeric, settled_at timestamptz,
  unique (bot, event_id, market));

create table if not exists edge_lab.game_results (
  event_id text primary key, league text, commence_time timestamptz, home_team text, away_team text,
  home_score int, away_score int, completed_at timestamptz);

create table if not exists edge_lab.fetch_log (
  fetched_at timestamptz primary key, events int, picks int, settled int, requests_remaining text, note text);

alter table edge_lab.odds_snapshots enable row level security;
alter table edge_lab.latest_odds enable row level security;
alter table edge_lab.bot_picks enable row level security;
alter table edge_lab.game_results enable row level security;
alter table edge_lab.fetch_log enable row level security;

-- Scheduling (applied separately; secrets are created in Vault, not stored here)
-- create extension if not exists pg_cron; create extension if not exists pg_net;
-- create function edge_lab.run_odds_job() ... net.http_post(.../functions/v1/edge-lab-odds, x-job-token from vault)
-- select cron.schedule('edge-lab-odds-morning', '0 16 * * *', 'select edge_lab.run_odds_job()');
-- select cron.schedule('edge-lab-odds-evening', '30 22 * * *', 'select edge_lab.run_odds_job()');

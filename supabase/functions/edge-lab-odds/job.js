// Edge Lab odds job (runs as a Supabase Edge Function, scheduled by pg_cron).
// 1) Pull NBA + NFL odds (moneyline, spread, total) from The Odds API -> edge_lab.odds_snapshots / latest_odds
// 2) Quant bot picks every game starting within PICK_WINDOW_HOURS (one pick per market)
// 3) When games have finished, pull final scores (edge_lab.game_results), grade Quant picks, record closing-line value
// Scores-only mode (runScores): live + recent scores -> edge_lab.live_scores (and finals -> game_results + grading).
//   Spends credits only for a league with a game in its live window or a started game still missing a result.
// Pure JS so it runs on Deno (Supabase) and Node (tests). `sql` is a postgres.js client; `fetchJson(url)` returns {data, remaining}.

export const SPORTS = { basketball_nba: "NBA", americanfootball_nfl: "NFL" };
export const MARKETS = ["h2h", "spreads", "totals"];
const PICK_WINDOW_HOURS = 7;
const LIVE_WINDOW_HOURS = { NBA: 3.5, NFL: 4.5 };

export const dec = (a) => (a > 0 ? 1 + a / 100 : 1 + 100 / Math.abs(a));
export const implied = (a) => 1 / dec(a);
export const ev = (p, a) => p * dec(a) - 1;

// books: {book: {market: [{name, price, point}]}} -> consensus view on the most common line
export function marketView(books, market) {
  let rows = [];
  for (const [book, mk] of Object.entries(books || {})) {
    const outs = mk[market] || [];
    if (outs.length === 2 && outs.every((o) => o.price != null)) rows.push([book, outs]);
  }
  if (!rows.length) return null;
  const key = (outs) => (market === "h2h" ? "" : JSON.stringify(outs.map((o) => [o.name, o.point == null ? null : Number(o.point)]).sort()));
  const counts = {};
  rows.forEach(([, o]) => (counts[key(o)] = (counts[key(o)] || 0) + 1));
  const common = Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
  rows = rows.filter(([, o]) => key(o) === common);
  const fairSum = {}, best = {}, points = {};
  for (const [book, outs] of rows) {
    const tot = outs.reduce((s, o) => s + implied(o.price), 0);
    for (const o of outs) {
      fairSum[o.name] = (fairSum[o.name] || 0) + implied(o.price) / tot;
      points[o.name] = o.point == null ? null : Number(o.point);
      if (!best[o.name] || dec(o.price) > dec(best[o.name][0])) best[o.name] = [o.price, book];
    }
  }
  const fair = {};
  for (const k in fairSum) fair[k] = fairSum[k] / rows.length;
  return { fair, best, points, booksUsed: rows.length };
}

// Quant: best EV at the best price; near-ties (<0.5% EV apart) follow the line move since open
export function quantPick(view, openingFair) {
  const sides = Object.keys(view.fair);
  if (sides.length !== 2) return null;
  const evs = Object.fromEntries(sides.map((s) => [s, ev(view.fair[s], view.best[s][0])]));
  const [a, b] = sides.sort((x, y) => evs[y] - evs[x]);
  if (evs[a] - evs[b] < 0.005 && openingFair) {
    const mv = (s) => view.fair[s] - (openingFair[s] ?? view.fair[s]);
    return mv(a) >= mv(b) ? a : b;
  }
  return a;
}

export function grade(market, side, point, home, away, hs, as) {
  let m;
  if (market === "h2h") { if (hs === as) return "push"; return (side === home) === (hs > as) ? "won" : "lost"; }
  if (market === "spreads") { const [mine, opp] = side === home ? [hs, as] : [as, hs]; m = mine + Number(point) - opp; }
  else { const t = hs + as; m = side.toLowerCase() === "over" ? t - Number(point) : Number(point) - t; }
  return m > 0 ? "won" : m < 0 ? "lost" : "push";
}
export const profitUnits = (r, a) => (r === "won" ? dec(a) - 1 : r === "lost" ? -1 : 0);

const r4 = (x) => Math.round(x * 10000) / 10000;

async function booksAt(sql, eventId, which) {
  const [{ ts }] = which === "open"
    ? await sql`select min(fetched_at) as ts from edge_lab.odds_snapshots where event_id=${eventId}`
    : await sql`select max(fetched_at) as ts from edge_lab.odds_snapshots where event_id=${eventId} and fetched_at < commence_time`;
  if (!ts) return null;
  const books = {};
  for (const r of await sql`select book, market, outcome, price, point from edge_lab.odds_snapshots where event_id=${eventId} and fetched_at=${ts}`) {
    ((books[r.book] ??= {})[r.market] ??= []).push({ name: r.outcome, price: r.price, point: r.point == null ? null : Number(r.point) });
  }
  return books;
}

const parseScores = (g) => {
  if (!g.scores) return [null, null];
  const sc = Object.fromEntries(g.scores.map((s) => [s.name, parseInt(s.score, 10)]));
  const hs = sc[g.home_team], as = sc[g.away_team];
  if (hs == null || as == null || Number.isNaN(hs) || Number.isNaN(as)) return [null, null];
  return [hs, as];
};

// Record a completed game's final, grade its open Quant picks, record CLV. Returns picks settled.
async function settleGame(sql, g, league, hs, as, now) {
  let settled = 0;
  await sql`insert into edge_lab.game_results values (${g.id}, ${league}, ${g.commence_time}, ${g.home_team}, ${g.away_team}, ${hs}, ${as}, ${now}) on conflict (event_id) do nothing`;
  const close = await booksAt(sql, g.id, "close");
  for (const p of await sql`select id, market, side, point, price from edge_lab.bot_picks where event_id=${g.id} and status='open'`) {
    const result = grade(p.market, p.side, p.point, g.home_team, g.away_team, hs, as);
    let closeFair = null, clv = null;
    const cv = close ? marketView(close, p.market) : null;
    const samePoint = p.market === "h2h" || (cv && cv.points[p.side] === (p.point == null ? null : Number(p.point)));
    if (cv && p.side in cv.fair && samePoint) { closeFair = r4(cv.fair[p.side]); clv = r4(closeFair * dec(p.price) - 1); }
    await sql`update edge_lab.bot_picks set status=${result}, profit_units=${r4(profitUnits(result, p.price))}, home_score=${hs}, away_score=${as},
      close_fair=${closeFair}, clv=${clv}, settled_at=${now} where id=${p.id}`;
    settled++;
  }
  return settled;
}

async function upsertLive(sql, g, league, hs, as, now) {
  await sql`insert into edge_lab.live_scores (event_id, league, commence_time, home_team, away_team, home_score, away_score, completed, last_update, fetched_at)
    values (${g.id}, ${league}, ${g.commence_time}, ${g.home_team}, ${g.away_team}, ${hs}, ${as}, ${!!g.completed}, ${g.last_update ?? null}, ${now})
    on conflict (event_id) do update set commence_time=excluded.commence_time, home_score=excluded.home_score, away_score=excluded.away_score,
      completed=excluded.completed, last_update=excluded.last_update, fetched_at=excluded.fetched_at`;
}

export async function runJob(sql, fetchJson, apiKey, now = new Date()) {
  let events = 0, picks = 0, settled = 0, remaining = null;
  const notes = [];
  const base = (sport, ep, params) => `https://api.the-odds-api.com/v4/sports/${sport}/${ep}?` + new URLSearchParams({ apiKey, ...params });

  // 1) odds + 2) quant picks
  for (const [sport, league] of Object.entries(SPORTS)) {
    let data;
    try { ({ data, remaining } = await fetchJson(base(sport, "odds", { regions: "us", markets: MARKETS.join(","), oddsFormat: "american" }))); }
    catch (e) { notes.push(`${league} odds failed: ${e.message}`); continue; }
    for (const e of data) {
      const books = {};
      const rows = [];
      for (const bm of e.bookmakers || []) {
        const b = (books[bm.title] ??= {});
        for (const mk of bm.markets || []) {
          b[mk.key] = mk.outcomes.map((o) => ({ name: o.name, price: o.price ?? null, point: o.point ?? null }));
          for (const o of mk.outcomes) rows.push({ fetched_at: now, league, event_id: e.id, commence_time: e.commence_time, home_team: e.home_team, away_team: e.away_team, book: bm.title, market: mk.key, outcome: o.name, price: o.price ?? null, point: o.point ?? null });
        }
      }
      if (rows.length) await sql`insert into edge_lab.odds_snapshots ${sql(rows)}`;
      await sql`insert into edge_lab.latest_odds (event_id, league, commence_time, home_team, away_team, fetched_at, books)
        values (${e.id}, ${league}, ${e.commence_time}, ${e.home_team}, ${e.away_team}, ${now}, ${sql.json(books)})
        on conflict (event_id) do update set commence_time=excluded.commence_time, fetched_at=excluded.fetched_at, books=excluded.books`;
      events++;
      const start = new Date(e.commence_time);
      if (!(start > now && start - now <= PICK_WINDOW_HOURS * 3600e3)) continue;
      const opening = await booksAt(sql, e.id, "open");
      for (const market of MARKETS) {
        const view = marketView(books, market);
        if (!view) continue;
        const ov = opening ? marketView(opening, market) : null;
        const side = quantPick(view, ov && ov.fair);
        if (!side) continue;
        const [price, book] = view.best[side];
        const res = await sql`insert into edge_lab.bot_picks (bot, event_id, league, market, side, point, price, book, fair_prob, ev, picked_at, commence_time, home_team, away_team)
          values ('quant', ${e.id}, ${league}, ${market}, ${side}, ${view.points[side]}, ${price}, ${book}, ${r4(view.fair[side])}, ${r4(ev(view.fair[side], price))}, ${now}, ${e.commence_time}, ${e.home_team}, ${e.away_team})
          on conflict (bot, event_id, market) do nothing`;
        picks += res.count;
      }
    }
  }

  // 3) scores -> results, grading, CLV (spends credits only when a started game lacks a result)
  for (const [sport, league] of Object.entries(SPORTS)) {
    const [{ n }] = await sql`select count(*)::int as n from edge_lab.latest_odds l where l.league=${league}
      and l.commence_time < ${new Date(now - 3 * 3600e3)} and l.commence_time > ${new Date(now - 3 * 86400e3)}
      and not exists (select 1 from edge_lab.game_results r where r.event_id=l.event_id)`;
    if (!n) continue;
    let games;
    try { ({ data: games, remaining } = await fetchJson(base(sport, "scores", { daysFrom: "3" }))); }
    catch (e) { notes.push(`${league} scores failed: ${e.message}`); continue; }
    for (const g of games) {
      const [hs, as] = parseScores(g);
      if (hs == null) continue;
      await upsertLive(sql, g, league, hs, as, now);
      if (g.completed) settled += await settleGame(sql, g, league, hs, as, now);
    }
  }
  await sql`delete from edge_lab.odds_snapshots where fetched_at < ${new Date(now - 45 * 86400e3)}`;
  await sql`delete from edge_lab.live_scores where commence_time < ${new Date(now - 4 * 86400e3)}`;
  await sql`insert into edge_lab.fetch_log values (${now}, ${events}, ${picks}, ${settled}, ${remaining}, ${notes.join("; ") || null})`;
  return { events, picks, settled, remaining, notes };
}

// Scores-only refresh (called by the feed task before it builds the app feed). No odds calls.
export async function runScores(sql, fetchJson, apiKey, now = new Date()) {
  let updated = 0, settled = 0, remaining = null;
  const notes = [], called = [];
  const base = (sport, ep, params) => `https://api.the-odds-api.com/v4/sports/${sport}/${ep}?` + new URLSearchParams({ apiKey, ...params });
  for (const [sport, league] of Object.entries(SPORTS)) {
    const live = new Date(now - LIVE_WINDOW_HOURS[league] * 3600e3);
    const [{ n }] = await sql`select count(*)::int as n from edge_lab.latest_odds l where l.league=${league}
      and l.commence_time <= ${now} and l.commence_time > ${new Date(now - 86400e3)}
      and not exists (select 1 from edge_lab.game_results r where r.event_id=l.event_id)
      and (l.commence_time > ${live} or not exists (select 1 from edge_lab.live_scores s where s.event_id=l.event_id and s.completed))`;
    if (!n) continue;
    let games;
    try { ({ data: games, remaining } = await fetchJson(base(sport, "scores", { daysFrom: "1" }))); called.push(league); }
    catch (e) { notes.push(`${league} scores failed: ${e.message}`); continue; }
    for (const g of games) {
      const [hs, as] = parseScores(g);
      if (hs == null) continue;
      await upsertLive(sql, g, league, hs, as, now);
      updated++;
      if (g.completed) settled += await settleGame(sql, g, league, hs, as, now);
    }
  }
  if (called.length || notes.length) {
    await sql`insert into edge_lab.fetch_log values (${now}, ${0}, ${0}, ${settled}, ${remaining}, ${["scores-only: " + (called.join(",") || "none"), ...notes].join("; ")})`;
  }
  return { mode: "scores", called, updated, settled, remaining, notes };
}

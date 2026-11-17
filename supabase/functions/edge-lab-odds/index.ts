import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.5";
import { runJob, runScores } from "./job.js";

// Called twice a day by pg_cron (full odds job), and by the feed task with {"mode":"scores"} (scores only).
// Auth: the caller must send the job token stored in Vault.
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// The Odds API occasionally returns a transient 5xx / 429 or drops the connection.
// Retry those up to 3 more times with backoff (2s, 6s, 15s). 4xx (bad key, out of credits) fail at once.
// Failed attempts that return an error status do not use credits.
async function fetchWithRetry(url: string) {
  const waits = [2000, 6000, 15000];
  let lastErr = "";
  for (let attempt = 0; attempt <= waits.length; attempt++) {
    if (attempt) await sleep(waits[attempt - 1]);
    try {
      const r = await fetch(url);
      if (r.ok) return r;
      lastErr = `HTTP ${r.status} ${(await r.text()).slice(0, 200)}`;
      if (r.status !== 429 && r.status < 500) break;
    } catch (e) {
      lastErr = `network error: ${String(e).slice(0, 200)}`;
    }
  }
  throw new Error(lastErr + " (after retries)");
}

Deno.serve(async (req: Request) => {
  const sql = postgres(Deno.env.get("SUPABASE_DB_URL")!, { prepare: false, max: 1 });
  try {
    const secrets = await sql`select name, decrypted_secret from vault.decrypted_secrets where name in ('edge_lab_odds_api_key','edge_lab_job_token')`;
    const get = (n: string) => secrets.find((s) => s.name === n)?.decrypted_secret;
    if (!get("edge_lab_job_token") || req.headers.get("x-job-token") !== get("edge_lab_job_token")) {
      return new Response("unauthorized", { status: 401 });
    }
    let mode = "full";
    try { mode = (await req.json())?.mode ?? "full"; } catch { /* empty body */ }
    const fetchJson = async (url: string) => {
      const r = await fetchWithRetry(url);
      return { data: await r.json(), remaining: r.headers.get("x-requests-remaining") };
    };
    const out = mode === "scores"
      ? await runScores(sql, fetchJson, get("edge_lab_odds_api_key")!)
      : await runJob(sql, fetchJson, get("edge_lab_odds_api_key")!);
    return Response.json(out);
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  } finally {
    await sql.end();
  }
});

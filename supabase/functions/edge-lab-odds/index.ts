import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import postgres from "npm:postgres@3.4.5";
import { runJob } from "./job.js";

// Called twice a day by pg_cron. Auth: the caller must send the job token stored in Vault.
Deno.serve(async (req: Request) => {
  const sql = postgres(Deno.env.get("SUPABASE_DB_URL")!, { prepare: false, max: 1 });
  try {
    const secrets = await sql`select name, decrypted_secret from vault.decrypted_secrets where name in ('edge_lab_odds_api_key','edge_lab_job_token')`;
    const get = (n: string) => secrets.find((s) => s.name === n)?.decrypted_secret;
    if (!get("edge_lab_job_token") || req.headers.get("x-job-token") !== get("edge_lab_job_token")) {
      return new Response("unauthorized", { status: 401 });
    }
    const fetchJson = async (url: string) => {
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
      return { data: await r.json(), remaining: r.headers.get("x-requests-remaining") };
    };
    const out = await runJob(sql, fetchJson, get("edge_lab_odds_api_key")!);
    return Response.json(out);
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  } finally {
    await sql.end();
  }
});

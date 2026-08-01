// The Actions dashboard's back end: read run status, and start a run.
//
//   GET  /wx-actions?op=status              -> [{file, name, last:{...}}]
//   POST /wx-actions {op:"run", file, inputs}  + header x-wx-key  -> {ok:true}
//
// Why a function at all: the page is a static file on GitHub Pages, so any
// token in it is public the moment it ships. The token lives here instead, as
// a Supabase secret, and this is the only thing that ever sees it.
//
// Which does not make the endpoint private — its URL is in the page source
// like every other one. So:
//
//   * only the workflows in ALLOW can be dispatched, by filename. Not "any
//     workflow in the repo", because the token can reach all of them.
//   * only the inputs each workflow actually declares are forwarded, so a
//     caller cannot smuggle a field the workflow was not expecting.
//   * status is readable by anyone who finds the URL. That is a list of job
//     names and timestamps for one weather repo, and it is the price of the
//     dashboard rendering without a login.
//   * starting a run needs WX_OPS_KEY. Without that anyone reading the page
//     source could spend the repo's Actions minutes, and there is no ceiling
//     on how often.
//
// Secrets (set in the Supabase dashboard, never in this file):
//   GH_TOKEN     fine-grained PAT for this repo, Actions: read and write.
//                "Workflows" permission is a different thing — it governs
//                editing workflow FILES, not starting runs.
//   WX_OPS_KEY   passphrase the drawer asks for once.

const OWNER = "raitisx";
const REPO = "baltic-wx-fusion";
const REF = "main";
const API = "https://api.github.com";

// filename -> inputs it is allowed to receive. Anything else is dropped.
const ALLOW: Record<string, string[]> = {
  "tick.yml": [],
  "models.yml": ["pairing"],
  "publish-app.yml": [],
  "backfill.yml": [
    "target", "hours", "force", "days", "runs", "lvgmc", "metar_start",
    "metar_end", "lt_days", "grid_from", "grid_to", "only_missing",
    "obs_prune",
  ],
};

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type, x-wx-key",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });

function gh(path: string, init: RequestInit = {}) {
  const token = Deno.env.get("GH_TOKEN") ?? "";
  return fetch(`${API}${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "baltic-wx-fusion-ops",
      Authorization: `Bearer ${token}`,
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(init.headers ?? {}),
    },
  });
}

// Length-independent compare. The timing of a string === on a short passphrase
// is not a realistic attack here, but it costs three lines not to think about
// it again.
function sameSecret(given: string, want: string): boolean {
  if (!want) return false;
  let diff = given.length ^ want.length;
  for (let i = 0; i < Math.max(given.length, want.length); i++) {
    diff |= (given.charCodeAt(i % (given.length || 1)) || 0) ^
            (want.charCodeAt(i % (want.length || 1)) || 0);
  }
  return diff === 0;
}

async function status() {
  // One call for the run history rather than one per workflow: fifty runs is
  // comfortably more than four workflows' worth of "most recent".
  const [wfR, runR] = await Promise.all([
    gh(`/repos/${OWNER}/${REPO}/actions/workflows?per_page=100`),
    gh(`/repos/${OWNER}/${REPO}/actions/runs?per_page=50`),
  ]);
  if (!wfR.ok) return json({ error: `workflows: HTTP ${wfR.status}` }, 502);
  const wfs = (await wfR.json()).workflows ?? [];
  const runs = runR.ok ? ((await runR.json()).workflow_runs ?? []) : [];
  const byId = new Map<number, string>();
  for (const w of wfs) {
    const file = String(w.path ?? "").split("/").pop() ?? "";
    if (file in ALLOW) byId.set(w.id, file);
  }
  // Every recent run, not just the newest per file. backfill.yml is thirteen
  // different jobs wearing one filename, and the only thing that tells them
  // apart is the run name — which the workflow now builds from its target.
  const out = [];
  for (const r of runs) {
    const file = byId.get(r.workflow_id);
    if (!file) continue;
    out.push({
      file,
      title: r.display_title ?? r.name ?? "",
      status: r.status,                   // queued | in_progress | completed
      conclusion: r.conclusion,           // success | failure | cancelled | ...
      started: r.run_started_at ?? r.created_at,
      ended: r.status === "completed" ? r.updated_at : null,
      event: r.event,
      url: r.html_url,
      number: r.run_number,
    });
  }
  const known = [...byId.values()].map((file) => ({ file }));
  return json({ runs: out, workflows: known, checked: new Date().toISOString() });
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  try {
    if (!Deno.env.get("GH_TOKEN")) {
      return json({ error: "GH_TOKEN is not set on this function" }, 503);
    }
    if (req.method === "GET") {
      const op = new URL(req.url).searchParams.get("op") ?? "status";
      if (op !== "status") return json({ error: "unknown op" }, 400);
      return await status();
    }
    if (req.method !== "POST") return json({ error: "method" }, 405);

    const key = req.headers.get("x-wx-key") ?? "";
    if (!sameSecret(key, Deno.env.get("WX_OPS_KEY") ?? "")) {
      return json({ error: "bad or missing key" }, 401);
    }
    const body = await req.json().catch(() => ({}));
    if (body.op !== "run") return json({ error: "unknown op" }, 400);
    const file = String(body.file ?? "");
    const allowed = ALLOW[file];
    if (!allowed) return json({ error: `${file} is not on the list` }, 400);

    // Only declared inputs, and only as strings — that is what the dispatch
    // API takes, and it is what the web UI sends for typed inputs too.
    const inputs: Record<string, string> = {};
    for (const k of allowed) {
      const v = (body.inputs ?? {})[k];
      if (v === undefined || v === null || v === "") continue;
      inputs[k] = String(v);
    }
    const r = await gh(
      `/repos/${OWNER}/${REPO}/actions/workflows/${file}/dispatches`,
      { method: "POST", body: JSON.stringify({ ref: REF, inputs }) },
    );
    if (r.status !== 204) {
      return json({ error: `dispatch: HTTP ${r.status} ${await r.text()}` }, 502);
    }
    // The run does not appear in the API instantly, so the page polls rather
    // than expecting this response to carry it.
    return json({ ok: true, file, inputs });
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});

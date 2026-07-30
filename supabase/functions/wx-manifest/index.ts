// Read-only CORS proxy for the baltic-wx public bucket's map manifests.
// r2.dev sends no CORS headers, which blocks browser fetch() of JSON.
// Serves ONLY whitelisted manifest/index paths (already-public data);
// no writes, no auth needed; verify_jwt off keeps requests preflight-free.
//
// Every map layer the page reads MUST be listed here. Leaving gfs out is what
// made the maps column stop at MEPS's +66 h horizon: the frames existed in the
// bucket, but the browser could not read the index that names them, so days
// 3-7 reported "no frame for this hour".

const BUCKET = "https://pub-29a41af0b6de4fe9a0d144b6a88fa144.r2.dev";
const ALLOWED = /^maps\/(meps|gfs|um4|um1|radar)\/(latest|archive)\.json$/;
const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (req.method !== "GET") {
    return new Response("method not allowed", { status: 405, headers: CORS });
  }
  const m = new URL(req.url).searchParams.get("m") ?? "";
  if (!ALLOWED.test(m)) {
    return new Response("forbidden", { status: 403, headers: CORS });
  }
  const r = await fetch(`${BUCKET}/${m}`);
  return new Response(await r.text(), {
    status: r.status,
    headers: {
      ...CORS,
      "Content-Type": "application/json",
      "Cache-Control": "public, max-age=120",
    },
  });
});

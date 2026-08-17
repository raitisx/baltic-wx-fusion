// Cloudflare Worker for meteo.ttaero.lv
//
// The whole site lives in the baltic-wx R2 bucket: the app page at key
// app/index.html (uploaded by .github/workflows/publish-app.yml), the maps
// under maps/, the per-point meteogram JSON under meteogram/, the manifests
// (latest.json / archive.json) beside them.
//
// This Worker serves the page at / and proxies every other path straight to the
// matching bucket key, same-origin. That is what lets the page read everything
// from https://meteo.ttaero.lv/... — no cross-origin CORS on the meteogram
// fetch — which is the branch the page takes for any `meteo.*` host (see
// MAPS_BASE in docs/index.html).
//
// Read-only: only GET/HEAD reach the bucket. Bind the bucket as BUCKET (see
// wrangler.toml) and put the route meteo.ttaero.lv/* on this Worker.

const PAGE_KEY = "app/index.html";

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", {
        status: 405,
        headers: { allow: "GET, HEAD" },
      });
    }

    const url = new URL(request.url);
    // "/"          -> app/index.html   (the page)
    // "/index.html"-> app/index.html   (same)
    // "/maps/x.png"-> maps/x.png        (bucket key, verbatim)
    let key = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
    if (key === "" || key === "index.html") key = PAGE_KEY;

    const object = await env.BUCKET.get(key);
    if (object === null) {
      return new Response("Not found", {
        status: 404,
        headers: { "cache-control": "no-store" },
      });
    }

    const headers = new Headers();
    // Content-Type, Content-Encoding, Cache-Control, etc. as stored on upload.
    object.writeHttpMetadata(headers);
    headers.set("etag", object.httpEtag);
    // Same-origin already, but keep it permissive so the objects stay usable
    // straight off the bucket URL too.
    headers.set("access-control-allow-origin", "*");
    // The page carries its own short TTL on upload; give it a floor here in
    // case an older object predates that.
    if (key === PAGE_KEY && !headers.has("cache-control")) {
      headers.set("cache-control", "public, max-age=120, must-revalidate");
    }

    return new Response(request.method === "HEAD" ? null : object.body, {
      headers,
    });
  },
};

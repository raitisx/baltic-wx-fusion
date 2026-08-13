// meteo-dashboard — serves the Baltic weather-fusion page and all of its
// imagery/data at ttaero.lv/meteo, from the baltic-wx R2 bucket.
//
// The page (app/index.html) and its assets (maps/…, meteogram/…, radar
// frames) all live in one bucket. Publishing them under a single origin —
// ttaero.lv/meteo — means the page's own meteogram fetch() is same-origin, so
// no bucket CORS policy is needed. Mirrors the gimbal-dashboard worker.
//
//   /meteo            -> app/index.html
//   /meteo/           -> app/index.html
//   /meteo/<key>      -> bucket object <key>   (maps/meps/…, meteogram/…, …)
//
// The page reads MAPS_BASE = <origin>/meteo when served here, so a request for
// `${MAPS_BASE}/maps/meps/…png` arrives as /meteo/maps/meps/…png and maps to
// the bucket key with the /meteo/ prefix stripped.

const PREFIX = "/meteo";
const APP_KEY = "app/index.html";
// The page is small and republished on every docs/index.html push; keep it
// briefly cached but revalidated so a new deploy shows up within a couple of
// minutes, exactly like the r2.dev copy (publish-app.yml sets the same).
const APP_CACHE = "public, max-age=120, must-revalidate";

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    let path = url.pathname;

    // Only GET/HEAD; this is a read-only mirror.
    if (req.method !== "GET" && req.method !== "HEAD") {
      return new Response("method not allowed", { status: 405 });
    }

    // Strip the mount prefix. Route patterns guarantee we only see /meteo*.
    if (path === PREFIX || path === PREFIX + "/") {
      return serveApp(req, env);
    }
    if (!path.startsWith(PREFIX + "/")) {
      // Shouldn't happen given the route, but be explicit.
      return new Response("not found", { status: 404 });
    }
    let key = decodeURIComponent(path.slice((PREFIX + "/").length));
    if (!key || key.includes("..")) {
      return new Response("bad key", { status: 400 });
    }
    // A bare /meteo/ (or any path resolving to empty) is the app too.
    if (key === "" ) return serveApp(req, env);

    const obj = await env.BUCKET.get(key);
    if (!obj) return new Response("not found", { status: 404 });
    const headers = new Headers();
    obj.writeHttpMetadata(headers);
    headers.set("etag", obj.httpEtag);
    // Fall back to a sensible cache if the object carries none. The map/radar
    // objects already set their own long max-age at write time; this only
    // fills gaps.
    if (!headers.has("cache-control")) {
      headers.set("cache-control", "public, max-age=300");
    }
    return new Response(req.method === "HEAD" ? null : obj.body, { headers });
  },
};

async function serveApp(req, env) {
  const obj = await env.BUCKET.get(APP_KEY);
  if (!obj) return new Response("app not published", { status: 503 });
  const headers = new Headers();
  headers.set("content-type", "text/html; charset=utf-8");
  headers.set("cache-control", APP_CACHE);
  headers.set("etag", obj.httpEtag);
  return new Response(req.method === "HEAD" ? null : obj.body, { headers });
}

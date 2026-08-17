# meteo.ttaero.lv Worker

Serves the weather app on its own subdomain, `meteo.ttaero.lv`, backed entirely
by the `baltic-wx` R2 bucket — the page at `/`, everything else proxied
same-origin to the matching bucket key.

## Why

The page (`docs/index.html`) picks where it reads maps and the meteogram JSON
from based on the host it's served from (`MAPS_BASE`):

- `ttaero.lv/meteo` → same-origin under the `/meteo` prefix (existing Worker).
- **`meteo.ttaero.lv` (any `meteo.*` host) → same-origin at the root** ← this Worker.
- anywhere else → the public `pub-…​.r2.dev` bucket URL (CORS `*`).

Serving same-origin means the meteogram `fetch()` (a real XHR, unlike the map
`<img>`s) never crosses an origin, and the maps get Cloudflare's cache in front
of R2.

## What it does

`meteo-worker.js`:

- `GET /` and `GET /index.html` → bucket key `app/index.html` (the page, which
  `publish-app.yml` uploads on every push to `docs/index.html`).
- `GET /<anything>` → bucket key `<anything>` verbatim — so
  `/maps/fused/latest.json`, `/meteogram/temp/1234.json`, `/maps/bg_3059.png`
  all resolve to the same objects the page already asks for.
- Anything other than GET/HEAD → 405. Missing key → 404.

Stored `Content-Type` / `Content-Encoding` / `Cache-Control` ride through
unchanged; `Access-Control-Allow-Origin: *` is added so the objects stay usable
off the bucket URL too.

## Deploy

```sh
cd worker
npm i -g wrangler      # if not already installed
wrangler login         # once, authorises against the Cloudflare account
wrangler deploy
```

`wrangler.toml` binds the `baltic-wx` bucket as `env.BUCKET` and registers
`meteo.ttaero.lv` as a **custom domain** on the Worker — Cloudflare provisions
the hostname, certificate, and proxied DNS record automatically, so there's no
separate DNS step. (`ttaero.lv` must already be a zone on the same Cloudflare
account, which it is.)

After deploy, open `https://meteo.ttaero.lv/` — the page loads, and the diag
line at the bottom should show maps/meteogram resolving from the subdomain
origin.

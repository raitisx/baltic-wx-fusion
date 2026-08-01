// Draw one historic MEPS hour, now, instead of queueing it for the next pass.
//
//   GET /wx-map?hour=20260622T08&thr=700   ->  image/png
//
// Why this exists: map frames are kept for a fortnight, the model grid behind
// them for a year. Asking for an older hour used to write a queue row and wait
// for the hourly job — correct, but an hour is a long time to look at a blank
// square. Everything needed to draw is in R2 already, so the work is a lookup
// table and a palette, which Deno can do in a few milliseconds.
//
// The three fixed pieces — the reprojection index, the border mask — are built
// once by wxfusion.meps_grid.build_static() and cached in module scope, so
// only the first request on a cold instance pays for them.
//
// Out of scope on purpose: hours older than the stored year. Those need an
// OPeNDAP read from MET Norway, which is Python's job; this returns 404 and
// the page falls back to the queue.

const BUCKET = "https://pub-29a41af0b6de4fe9a0d144b6a88fa144.r2.dev";
const PREFIX = "maps/meps_grid";
const W = 570, H = 690;
const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

// Same rules as maps_meps: a ceiling needs broken low cloud, fog is its own
// thing, and anything on the deck counts as fog whatever the fog field says.
const CEILING_LCC = 60;      // percent, stored as uint8
const FOG_COVER = 60;
const FOG_CEILING_M = 60;
const CLEAR = 65535;

// Palette: 0 transparent, 1 pink ceiling, 2 orange fog, 3-8 rain light->heavy,
// 9 border halo, 10 border core. Matches RAIN_STEPS and the pink/orange in
// maps_meps, so an on-demand frame is indistinguishable from a rendered one.
const PALETTE: [number, number, number, number][] = [
  [0, 0, 0, 0],
  [243, 184, 180, 191],
  [232, 163, 61, 204],
  [205, 234, 176, 160],
  [143, 208, 106, 195],
  [70, 177, 60, 225],
  [242, 212, 58, 240],
  [243, 148, 31, 250],
  [226, 86, 15, 255],
  [247, 241, 226, 230],
  [85, 80, 63, 255],
];
const RAIN_EDGES = [0.1, 0.4, 1.0, 2.0, 4.0, 8.0];   // mm/h -> classes 3..8

async function inflate(buf: ArrayBuffer): Promise<Uint8Array> {
  const s = new Blob([buf]).stream().pipeThrough(new DecompressionStream("deflate"));
  return new Uint8Array(await new Response(s).arrayBuffer());
}

type Wxg = { arrays: Record<string, ArrayBufferView>; meta: Record<string, unknown> };

async function readWxg(url: string): Promise<Wxg | null> {
  const r = await fetch(url);
  if (!r.ok) return null;
  const raw = await inflate(await r.arrayBuffer());
  const dv = new DataView(raw.buffer, raw.byteOffset);
  if (String.fromCharCode(...raw.slice(0, 4)) !== "WXG1") return null;
  // 4 magic + 4 length + 8 pad, and every array padded out to 8 as well, so
  // each section starts on an 8-byte boundary. Typed-array views in JS refuse
  // any other offset.
  const hlen = dv.getUint32(4, true);
  const header = JSON.parse(new TextDecoder().decode(raw.slice(16, 16 + hlen)));
  const arrays: Record<string, ArrayBufferView> = {};
  let off = 16 + hlen;
  for (const spec of header.arrays) {
    const n = spec.shape.reduce((a: number, b: number) => a * b, 1);
    const base = raw.buffer, at = raw.byteOffset + off;
    let a: ArrayBufferView;
    if (spec.dtype === "|u1") { a = new Uint8Array(base, at, n); }
    else if (spec.dtype === "<u2") { a = new Uint16Array(base, at, n); }
    else if (spec.dtype === "<u4") { a = new Uint32Array(base, at, n); }
    else if (spec.dtype === "<f4") { a = new Float32Array(base, at, n); }
    else throw new Error("dtype " + spec.dtype);
    arrays[spec.name] = a;
    off += a.byteLength + ((-a.byteLength) & 7);
  }
  return { arrays, meta: header.meta };
}

// --- minimal indexed PNG ----------------------------------------------------
function crc32(b: Uint8Array): number {
  let c = ~0;
  for (let i = 0; i < b.length; i++) {
    c ^= b[i];
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xEDB88320 & -(c & 1));
  }
  return ~c >>> 0;
}

async function png(indexed: Uint8Array, w: number, h: number): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  const put = (type: string, data: Uint8Array) => {
    const out = new Uint8Array(12 + data.length);
    const dv = new DataView(out.buffer);
    dv.setUint32(0, data.length);
    out.set(new TextEncoder().encode(type), 4);
    out.set(data, 8);
    dv.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
    chunks.push(out);
  };
  const ihdr = new Uint8Array(13);
  const dv = new DataView(ihdr.buffer);
  dv.setUint32(0, w); dv.setUint32(4, h);
  ihdr[8] = 8; ihdr[9] = 3;                    // 8-bit, palette
  put("IHDR", ihdr);
  const plte = new Uint8Array(PALETTE.length * 3);
  const trns = new Uint8Array(PALETTE.length);
  PALETTE.forEach((c, i) => {
    plte[i * 3] = c[0]; plte[i * 3 + 1] = c[1]; plte[i * 3 + 2] = c[2];
    trns[i] = c[3];
  });
  put("PLTE", plte);
  put("tRNS", trns);
  // filter byte 0 in front of every row, then deflate
  const rows = new Uint8Array((w + 1) * h);
  for (let y = 0; y < h; y++) {
    rows[y * (w + 1)] = 0;
    rows.set(indexed.subarray(y * w, (y + 1) * w), y * (w + 1) + 1);
  }
  const z = new Response(
    new Blob([rows]).stream().pipeThrough(new CompressionStream("deflate")));
  put("IDAT", new Uint8Array(await z.arrayBuffer()));
  put("IEND", new Uint8Array(0));
  const sig = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  const total = sig.length + chunks.reduce((a, c) => a + c.length, 0);
  const out = new Uint8Array(total);
  out.set(sig); let at = sig.length;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

// --- fixed assets, fetched once per instance --------------------------------
let INDEX: Uint32Array | null = null;
let BORDER: Uint8Array | null = null;

async function statics() {
  if (INDEX && BORDER) return;
  const [ix, bd] = await Promise.all([
    readWxg(`${BUCKET}/${PREFIX}/index.wxg`),
    readWxg(`${BUCKET}/${PREFIX}/borders.wxg`),
  ]);
  if (!ix || !bd) throw new Error("static assets missing — run build_static()");
  INDEX = ix.arrays.idx as Uint32Array;
  BORDER = bd.arrays.mask as Uint8Array;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  const url = new URL(req.url);
  const hour = (url.searchParams.get("hour") ?? "").replace(/[^0-9T]/g, "");
  const thr = Number(url.searchParams.get("thr") ?? "700");
  if (!/^\d{8}T\d{2}$/.test(hour)) {
    return new Response("bad hour", { status: 400, headers: CORS });
  }
  try {
    await statics();
    const y = hour.slice(0, 4), m = hour.slice(4, 6), d = hour.slice(6, 8);
    const g = await readWxg(`${BUCKET}/${PREFIX}/${y}/${m}/${d}/${hour}.wxg`);
    if (!g) {
      // Older than the stored grid: Python has to fetch it from MET.
      return new Response(JSON.stringify({ error: "no grid for that hour" }), {
        status: 404,
        headers: { ...CORS, "Content-Type": "application/json" },
      });
    }
    const cb = g.arrays.cb as Uint16Array;
    const lcc = g.arrays.lcc as Uint8Array;
    const fog = g.arrays.fog as Uint8Array;
    const pr = g.arrays.pr as Uint16Array;

    const out = new Uint8Array(W * H);
    for (let i = 0; i < W * H; i++) {
      const src = INDEX![i];
      if (src !== 0xFFFFFFFF) {
        const base = cb[src];
        const isFog = fog[src] >= FOG_COVER ||
          (base !== CLEAR && base < FOG_CEILING_M && lcc[src] >= CEILING_LCC);
        if (isFog) out[i] = 2;
        else if (base !== CLEAR && base < thr && lcc[src] >= CEILING_LCC) out[i] = 1;
        const rate = pr[src] / 100;
        if (rate >= RAIN_EDGES[0]) {
          let k = 3;
          for (let e = 1; e < RAIN_EDGES.length; e++) if (rate >= RAIN_EDGES[e]) k = 3 + e;
          out[i] = k;                      // rain draws over the tint, as in Python
        }
      }
      const b = BORDER![i];
      if (b) out[i] = b === 2 ? 10 : 9;
    }
    const body = await png(out, W, H);
    return new Response(body, {
      headers: {
        ...CORS,
        "Content-Type": "image/png",
        // Immutable: a past hour's model fields never change.
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Wx-Run": String(g.meta.run ?? ""),
      },
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: String(e) }), {
      status: 500, headers: { ...CORS, "Content-Type": "application/json" },
    });
  }
});

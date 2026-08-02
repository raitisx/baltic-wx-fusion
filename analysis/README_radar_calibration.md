# Putting the three radars on one scale

*2 August 2026.*

## The complaint

A composite frame in which most of the Baltics is one flat slab of `#00e300`,
with a step change in colour at the Latvian–Lithuanian border. Earlier, the
same thing seen from the other side: "Riga radar looks like it is less
sensitive than the LT one, which creates dark green instead of orange."

Both are the same fault. The classifier read hue, not intensity.

## What was actually happening

Measured on 141 archived frames plus 8 768 gauge-hours, 26 July – 2 August.

**Within a feed, the gradation was being thrown away.** Each national product
paints a fine ramp — Latvia 15 steps, Estonia 10, Lithuania 14 — and the hue
rules mapped all of them onto five classes by asking questions like "is green
≥ red and > blue". Result:

| feed | echo in one class | which |
|---|---|---|
| LT | 68 % | class 3, six ramp steps merged |
| EE | 64 % | class 2, three ramp steps merged |
| LV | 57 % | class 1, four ramp steps merged |

Latvia was worse than flat. Its ramp runs dark blue → cyan → white → yellow →
red, and the hue tests split it in an order set by where the thresholds
happened to fall: `#0000c7` and `#86f0fe` — opposite ends of the blue family —
both landed in class 1, while `#1aa2fe` between them landed in class 2.

**Across feeds, the same rain changed colour at the border.** On pixels where
two radars both painted echo, Lithuania read a full class hotter than Latvia:
mean class difference **+1.00** over 114 358 overlapping pixels.

## The two halves of the fix

### Order, from the imagery

Which step of a ramp is stronger is not something to guess from hue. Rank the
steps by *which colours border which* — accumulate the four-neighbour
composition of every palette colour over a few hundred frames — and exactly one
chain is consistent with the result, per feed. Latvia:

```
#0000c7 → #0034fe → #0079fe → #1aa2fe → #53d0fe → #86f0fe → #fefefe
        → #fef7c0 → #fee500 → #febc00 → #fe7300 → #fe3f00 → #c70000
        → #a00000 → #800000
```

The weakest step is identifiable independently: it is the one with the most
transparent neighbours, because it rings the echo. For Latvia that is `#0000c7`
at 21.3 % transparent against 1.1–8.1 % for everything else — so the blue
family runs dark-to-pale with *increasing* intensity, which is the opposite of
the assumption the hue rules encoded. The painted area falls monotonically
along the chain (19.0 % → 0.4 %), and the gauge means rise along it. Three
independent signals, one order.

Two extra Lithuanian steps turned up this way that the palette sweep had
missed as noise: `#fe8c00` (13 px in 38 frames) and `#f30f00` (52 px). They
are the top of that scale and matter more than their pixel count suggests.

An earlier attempt to order the steps by how deep inside the echo they sit —
normalised distance transform — agrees for Latvia and Estonia and inverts for
Lithuania, because Lithuania's heavy steps occur in small convective cells
where every pixel is near an edge. The neighbour chain is the sounder
statistic and it is the one used.

### Rate, from the gauges

For each of the 8 768 gauge-hours in a raining hour, take the colour that its
own national radar painted most often over that station across the hour, in a
5 km box. Then the mean hourly total under each step.

- **Own radar only.** A Latvian gauge calibrates the Latvian radar. The
  alternative — calibrating feeds against each other where they overlap — was
  tried, measured, and fails on geometry: the only place two of these radars
  see the same sky is a border strip where both are at 150–200 km, looking
  through the top of the weather. 49 952 pixel pairs produced a flat line.
- **The mean, not the median.** Under any light-rain step half the hours
  record nothing — the beam overshot, or it evaporated on the way down — so
  the median is 0.0 for steps that plainly carry rain. A 10 % top-trimmed mean
  is worse in the other direction: it puts Estonia's third step at 0.01 mm/h.
- **Only steps with ≥ 8 gauge-hours become knots**; between them the rate is
  interpolated geometrically, and the finished curve is forced monotone along
  the chain. Latvia's `#800000`, with one gauge-hour recording 0.0 mm, cannot
  drag the top of the scale to zero.
- **The last step of every ramp is pinned at 20 mm/h.** These are the colours
  the products reserve for extreme rain and no gauge has ever stood under one.
  Extrapolating each feed's own measured slope instead put Estonia's magenta
  at 2 mm/h — the same class as its yellow. For a flight decision, under-reading
  a downpour is the expensive direction to be wrong in.

The table is `wxfusion/assets/radar_rates.json`, bundled so it needs no
network and no credentials. The `radar-calibrate` job rebuilds it from more
weather and writes the result to R2, where it overrides the bundled copy.

## Result

| | LT–LV | EE–LV | EE–LT |
|---|---|---|---|
| mean class gap, before | **+1.00** | +1.21 | +0.17 |
| mean class gap, after | **+0.01** | −1.37 | −0.70 |
| mean absolute gap, before | 1.48 | 1.60 | 0.50 |
| mean absolute gap, after | **0.90** | 1.43 | 0.79 |

The border the complaint was about is fixed: Lithuania and Latvia now read the
same class on the same pixel. Per-feed class distributions on a live frame,
with no echo pixel gained or lost:

```
             c1     c2     c3     c4     c5
EE  before  0.0%  46.1%  43.4%  10.5%   0.0%
     after 66.2%  32.4%   1.4%   0.0%   0.0%
LT  before 19.5%  23.1%  56.0%   1.3%   0.0%
     after 45.1%  24.9%  15.1%  14.3%   0.6%
LV  before 60.8%  28.6%   0.0%   9.4%   0.8%
     after  0.0%  55.7%  42.3%   2.1%   0.0%
```

## What is not fixed, and why

**Estonia now sits about 0.7 of a class below the other two.** That is what
its gauges say — its two commonest steps, 54 % of its painted area, both
average 0.31 mm/h — and its product does paint very large areas of very light
echo. It may also be an artefact of which Estonian stations report
precipitation: the feed mixes synoptic gauges with bog-research plots at
shared coordinates. Not resolved.

**Latvia has no class-1 area at all**, because its weakest painted step
already measures 0.6 mm/h. There is supporting evidence that this is real:
where Latvia's radar paints nothing, gauges still record rain 8 % of the time
against 5 % (EE) and 3 % (LT), so its display floor genuinely sits higher.
But it also means a visible step remains at the Estonian border.

**The heavy end rests on one event each.** Leave-one-day-out: Lithuania's
`#ffe600` is 7.25 mm/h over the whole week and 2.66 without 1 August — a class.
The well-travelled light steps move by ±20 % or less. Only more weather fixes
this, which is what the recurring job is for.

**Latvia's gauge history cannot be extended.** The LVGMC 365-day archive
resource carries wind, visibility, temperature, humidity, pressure, snow depth,
cloud and lightning — 3.5 M rows, seventeen parameters, and no precipitation
at all. (`LITOT`/`LIGC`/`LICC`/`LI10I` look like rain and are lightning strike
counts: 885 cloud-to-cloud and 131 cloud-to-ground at Skulte during the hour
its gauge caught 21.9 mm.) Latvian pairs therefore only accumulate forward
from 26 July. Lithuania's history *is* reachable — meteo.lt serves hourly
observations back at least 200 days — and the radar overlay archive reaches
back at least 120, so a Lithuanian backfill is the cheapest available
improvement and is not done here.

## Incidental

`wx.points` held no coordinates for any of the 240 national stations, so the
gauge join returned nothing and `radar-calibrate` could never have produced a
table. `run_stations.py` had simply never run. It is wired ahead of the
calibrate step in `models.yml`, so the first daily model pass fixes it.

`stations.py` did not strip station names while the observation ingest did, so
`Tuulemäe ` in `wx.points` could never join to `Tuulemäe` in
`wx.observations_h` — one station lost, silently. Fixed.

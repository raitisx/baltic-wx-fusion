# The three radar feeds are not on one intensity scale

Asked whether the national radars are calibrated to each other. They are not,
and our own classifier was making the gap wider. Nothing here is wired into the
pipeline yet — see "why this is not shipped" at the end.

## What meteolapa publishes

`meteolapa_radar_scale.png` is their own legend, three bars against one mm/h
axis. Sampled into `../wxfusion/assets/radar_scales.json`. Read off it:

| rate | Estonia | Latvia | Lithuania |
|-----:|---------|--------|-----------|
| 0.2 mm/h | green | dark blue | blue |
| 2 mm/h | bright green | pale cyan | blue |
| 5 mm/h | yellow | yellow | blue |
| 8 mm/h | amber | amber | cyan |
| 20 mm/h | orange | maroon | green |

Lithuania is still blue at 5 mm/h where Latvia has been yellow since 5 and
Estonia since 2. Lithuania's *green* is 15-25 mm/h — heavy rain.

## What our classifier does with that

It classifies by hue, uniformly, with no idea which feed a pixel came from.
Against the published scale, on the colours actually seen in the 01.08 15:12
frames:

| feed | colour | published rate | class we give | class it should be |
|------|--------|---------------:|--------------:|-------------------:|
| LT | `#00c6c6` | 8.7 mm/h | 1 | 5 |
| LT | `#00c027` | 20.8 mm/h | 3 | 6 |
| LV | `#86f0fe` | 1.7 mm/h | 1 | 3 |
| LV | `#fefefe` | 2.9 mm/h | 6 | 4 |
| EE | `#4fb6d6` | 0.06 mm/h | 2 | none, below the draw threshold |

The `greenish` rule is the single line that answers the original question:
every shade of green becomes class 3, so Lithuania's 20 mm/h core and its
1 mm/h fringe come out the same colour. Latvia's white was being read as the
top of its scale; the legend puts it at 3 mm/h, in the gap between the blues
and the yellows.

## The rings

Estonia's palest step, `#4fb6d6`, is 0.06 mm/h. In the 01.08 composite it
formed a band 108-237 km from the Estonian antennas, density rising with range
and peaking at 230-240 km — 9.8% of the annulus, against under 2% anywhere
inside 210 km. Rain does not organise itself into a ring centred on a radar.
At 230 km the beam centre is around 5 km up, so a trace return there is not
surface precipitation.

## Why this is not shipped

Applying the published scale directly makes the feeds agree *worse*, not
better. Measured on 17,717 pixels where Latvia and Lithuania both report echo
in the same frame:

| | mean class LV | mean class LT | gap | agree within 1 class |
|---|---:|---:|---:|---:|
| hue rules (today) | 1.85 | 2.72 | +0.87 | 43% |
| published scale | 2.30 | 5.67 | +3.37 | 4% |

Latvia's colours match its legend row exactly (nearest-colour distance 0), so
that row is right. Lithuania's cannot be: taken at face value it puts the whole
of Lithuania above 8 mm/h, and Latvia's radar looking at the same sky says 2.
Either the legend's LT row describes a different product from the tiles served,
or it is out of date.

The fix is a cross-calibration from the overlap rather than from the legend:
one frame already gives 17,717 pixels where two radars see the same rain, and
Latvia is the one whose scale checks out. Collect those pairs over a range of
weather and the LT and EE colour-to-class tables fall out of the data.

---

# Attempt 2: cross-calibrate from the overlap. It does not work.

Harvested every pixel where Latvia and another feed reported echo in the same
frame, over 45 frames spanning a week, taking Latvia's rate as known.
49,952 pairs for Lithuania, 18,400 for Estonia.

The result is a flat line. Lithuania's entire colour range — cyan, through
every green, to yellow — maps to Latvian rates between **1.34 and 2.19 mm/h**.
Estonia's, including its red, maps to between **0.72 and 1.34**. Colour
carries almost no information about the rate the other radar reports.

That is geometry, not noise. The only place two of these radars see the same
sky is a narrow border strip where *both* are at 150-200 km, which is where
both are looking through the top of the weather. The overlap is the worst
place in the domain to calibrate, not the best. I had this backwards.

It does settle one thing. The legend claims Lithuanian cyan is 8.7 mm/h;
Latvia's radar, looking at the same rain, says 0.7. A factor of twelve over
49,952 pixels. Whatever meteolapa's LT row describes, it is not the tiles they
serve — so not shipping attempt 1 was right for a better reason than I had.

# What will work: the gauges

A rain gauge sits under one radar at short range and measures what reached the
ground. No overlap needed, no long-range geometry, and it calibrates all three
feeds by the same method against the same kind of truth.

The observations are already in the database:

| network | raining hours | ≥2 mm/h | ≥6 mm/h | stations | max |
|---------|--------------:|--------:|--------:|---------:|----:|
| meteo.lt | 460 | 57 | 7 | 52 | 14.1 mm/h |
| LVĢMC | 278 | 56 | 18 | 33 | 24.2 mm/h |
| ilmateenistus | 215 | 47 | 12 | 68 | 12.4 mm/h |

That covers the heavy end the overlap never reached, which is the end the whole
question is about.

One thing is missing. `wx.points` carries coordinates for METAR, grid and
virtual points only; the three national networks are stored in
`wx.observations_h` by `station_id` with nowhere to look up where they are.
Estonia's ingest already parses lat/lon and throws it away
(`ee_xml.py`, line 53); LVĢMC and meteo.lt do not parse it at all. So the next
step is: extract station coordinates in all three ingests, write them into
`wx.points`, then sample each feed's own colour at its own country's stations
for every raining hour and read the ramp straight off the pairs.

---

# Attempt 3: the gauges. This works, and it needs time.

Station coordinates now exist (`wxfusion/ingest/stations.py`, 240 sites:
34 LVĢMC, 52 meteo.lt, 154 ilmateenistus), so a gauge reading can be matched to
the radar pixel above it. Ran over 30 raining hours, 3,554 gauge readings.

**The legend's order is right.** Ranking each feed's colours by where they sit
on its own published ramp, against what the gauges under them measured:

| feed | colours | Spearman |
|------|--------:|---------:|
| Lithuania | 12 | +0.93 |
| Estonia | 7 | +0.89 |
| Latvia | 6 | +0.77 |

**The legend's rates are not.** Lithuania's cyan, which the legend puts at
8.7 mm/h: the gauges under it recorded a mean of 0.18-0.22 mm/h, and the
cross-radar attempt independently said 0.72. Three ways of asking, two of them
agreeing on "under 1", one of them saying "nearly nine".

Sanity check passes: where a feed painted nothing, 4-9% of gauge-hours were wet;
under each feed's strongest sampled colour, 80-100% were.

## What is still missing

Every feed's *sampled* range tops out between 1.4 and 2.5 mm/h, because in 30
hours no heavy core happened to sit over a gauge. Fitting the ramp anyway
(log-linear in position, weighted by sample count) extrapolates Latvia's whole
scale into 0.9-4.2 mm/h, which cannot be right — its top step is maroon, and
the legend calls that 50 mm/h.

So the tables are not written yet, and the classifier still uses the hue rules.
What exists is the machinery: `radar-calibrate` in the backfill workflow pairs
gauge-hours with radar colours and accumulates them in R2 across runs. Run it
on a schedule and the heavy end fills in as heavy rain falls; when each feed's
top third has a few hundred samples, the ramp can be fitted properly and wired
into `_classify_intensity`.

That is slower than a one-shot fix and it is the honest shape of the problem:
you cannot calibrate a radar against rain that has not fallen yet.

---

# The Estonian rings: five attempts, and what actually shipped

| attempt | why it failed |
|---------|---------------|
| flat range cut on weak echo | removed 73% of Latvia's echo, which is real — Latvia has one radar, so most of the country *is* long range |
| drop isolated all-weak blobs | removed 89% of Estonia on a day its whole field was genuinely light rain |
| per-frame ring detector | the ring in the reported frame is 110 km wide, not a narrow spike; the test missed it and false-fired on real rain drifting out |
| persistent clutter map | 60 frames over four days: median echo frequency beyond 200 km is **0.00**. The rings are episodic, not fixed — nothing to subtract |
| beam-height quality mask | physically right, but at the 4 km height that catches 72% of the ring it also takes 60% of Latvia |

On a single frame an artefact ring and real drizzle at 220 km are the same
picture. What separates the feeds is not the frame, it is **how many radars
each country has**, measured against the borders file:

| country | radars | land within 150 km | 180 km | 250 km |
|---------|-------:|-------------------:|-------:|-------:|
| Estonia | 2 | 94% | 98% | 100% |
| Lithuania | 2 | 96% | 100% | 100% |
| Latvia | 1 | 64% | 78% | 99% |

Estonian echo beyond 180 km is almost never over Estonia — it is over the sea,
or over Latvia where Latvia's own radar is looking at the same sky from closer.
Latvia's long range is not spare.

So: weak echo (classes 1-2 only) is limited per feed — 180 km for Estonia and
Lithuania, 250 km for Latvia — faded in over the last 40 km so there is no rim.
Strong returns are untouched at every range, because a heavy core at 230 km is
real weather even with the beam five kilometres up. Verified: class 3 and above
byte-identical for all three feeds, and the far arcs gone from the Estonian
frame while its rain core is unchanged.

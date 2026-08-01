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

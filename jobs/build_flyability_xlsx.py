"""Build the Limbazi flight-availability workbook.

Thresholds live in the Settings sheet and every verdict is a formula that
reads them, so the judgement can be re-made without refetching anything.

Wet ground follows two different rules:
  September  — wet for N hours after rain (N = 3).
  October    — rain wets the ground for the REST of that day, and whether it
               is still wet tomorrow depends on when the rain stopped:
               before 15:00 it dries overnight, at or after 15:00 it does not.
"""
import csv
import datetime as dt
import os

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import FormulaRule

SRC = "/home/user/baltic-wx-fusion/data/flyability"
OUT = ("/tmp/claude-0/-home-user-baltic-wx-fusion/"
       "6f66ee27-abc3-52e6-a004-8cc7d28a1022/scratchpad/build/"
       "Limbazi_flight_availability_2026-09-01_2026-11-01.xlsx")

FONT = "Arial"
D0, D1 = dt.date(2026, 9, 1), dt.date(2026, 11, 1)
LEAD = dt.date(2026, 8, 20)
BUILT = dt.date(2026, 10, 31)

HDR = PatternFill("solid", fgColor="1F3864")
SUB = PatternFill("solid", fgColor="D9E2F3")
INPUT = PatternFill("solid", fgColor="FFFF00")
GOOD = PatternFill("solid", fgColor="C6EFCE")
BAD = PatternFill("solid", fgColor="FFC7CE")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def fl(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


hourly = []
for r in csv.DictReader(open(f"{SRC}/limbazi_hourly.csv")):
    if r["temperature_2m"] in ("", None):
        continue
    t = dt.datetime.fromisoformat(r["time"])
    if not (LEAD <= t.date() <= D1):
        continue
    hourly.append({
        "t": t, "temp": fl(r["temperature_2m"]), "dew": fl(r["dew_point_2m"]),
        "prcp": fl(r["precipitation"]), "lowcc": fl(r["cloud_cover_low"]),
        "totcc": fl(r["cloud_cover"]), "gust": fl(r["wind_gusts_10m"]),
        "wspd": fl(r["wind_speed_10m"]),
    })
hourly.sort(key=lambda x: x["t"])
sun = {}
for r in csv.DictReader(open(f"{SRC}/limbazi_daily.csv")):
    sun[dt.date.fromisoformat(r["date"])] = (
        dt.datetime.fromisoformat(r["sunrise"]),
        dt.datetime.fromisoformat(r["sunset"]))
print(f"{len(hourly)} hours {hourly[0]['t']} .. {hourly[-1]['t']}")

wb = Workbook()

# ==================================================================== Settings
st = wb.active
st.title = "Settings"
st.sheet_view.showGridLines = False
st["A1"] = "Flight-availability thresholds — Limbaži"
st["A1"].font = Font(FONT, size=14, bold=True, color="1F3864")
st["A2"] = ("Edit the yellow cells. Every verdict in 'Days' and 'Hourly' is a "
            "formula that reads them, so the whole table re-decides itself.")
st["A2"].font = Font(FONT, size=9, italic=True, color="595959")

rows = [
    ("Flight altitude", 700, "m AGL", "The altitude you asked about."),
    ("Clearance needed below cloud", 300, "m",
     "VFR vertical separation from cloud; 300 m = 1000 ft."),
    ("→ Minimum cloud base required", "=B4+B5", "m AGL",
     "Computed: flight altitude + clearance."),
    ("Max wind gust", 12, "m/s",
     "ASSUMPTION — you did not give a number. 12 m/s ≈ 23 kt. "
     "Period p90 is 10.8 m/s, so this bites on roughly the windiest 7% of hours."),
    ("Min temperature", 0, "°C",
     "ASSUMPTION — frost/icing floor. Period minimum was +0.6 °C, "
     "so this never actually excluded an hour."),
    ("Max rain in the hour", 0, "mm", "Any measurable rain stops flying."),
    ("Low cloud that counts as a ceiling", 50, "%",
     "A ceiling is the lowest BKN/OVC layer. Below this, low cloud is "
     "scattered and imposes no ceiling."),
    ("Ground dry-out — September", 3, "h",
     "Your rule: the ground is wet for 3 h after rain, then flyable again "
     "the same day."),
    ("October rain cutoff hour", 15, ":00",
     "Your rule: in October, rain that stops BEFORE this hour dries "
     "overnight and the next day flies; rain at or after it leaves the "
     "ground wet all through the next day."),
    ("Minimum usable window", 3, "h",
     "Your rule: at least 3 h. Measured as CONSECUTIVE hours, "
     "not a daily total."),
    ("Daylight only (yes/no)", "yes", "",
     "Restricts flyable hours to between sunrise and sunset."),
]
st["A3"], st["B3"], st["C3"], st["D3"] = "Parameter", "Value", "Unit", "Note"
for c in "ABCD":
    st[f"{c}3"].font = Font(FONT, bold=True, color="FFFFFF")
    st[f"{c}3"].fill = HDR
for i, (lab, val, unit, note) in enumerate(rows, start=4):
    st[f"A{i}"], st[f"B{i}"], st[f"C{i}"], st[f"D{i}"] = lab, val, unit, note
    st[f"A{i}"].font = Font(FONT, bold=lab.startswith("→"))
    is_formula = isinstance(val, str) and val.startswith("=")
    st[f"B{i}"].font = Font(FONT, bold=True,
                            color="000000" if is_formula else "0000FF")
    if not is_formula:
        st[f"B{i}"].fill = INPUT
    st[f"C{i}"].font = Font(FONT)
    st[f"D{i}"].font = Font(FONT, size=9, color="595959")
    st[f"D{i}"].alignment = Alignment(wrap_text=True, vertical="top")
    for c in "ABCD":
        st[f"{c}{i}"].border = BOX
st.column_dimensions["A"].width = 34
st.column_dimensions["B"].width = 10
st.column_dimensions["C"].width = 8
st.column_dimensions["D"].width = 78
for i in range(4, 4 + len(rows)):
    st.row_dimensions[i].height = 32

MINCB = "Settings!$B$6"
MAXG, MINT, MAXP = "Settings!$B$7", "Settings!$B$8", "Settings!$B$9"
CCGATE, DRYSEP, OCTCUT = "Settings!$B$10", "Settings!$B$11", "Settings!$B$12"
MINWIN, DAYONLY = "Settings!$B$13", "Settings!$B$14"

# ====================================================================== Hourly
hs = wb.create_sheet("Hourly")
cols = [
    ("Time (local)", 17), ("Date", 11), ("Hour", 6),
    ("Temp °C", 9), ("Dewpt °C", 9), ("Rain mm", 9),
    ("Low cloud %", 11), ("Cloud %", 9), ("Gust m/s", 10), ("Wind m/s", 10),
    ("Sunrise", 14), ("Sunset", 14),
    ("Cloud base m", 12),
    ("Rain prev 3h", 12), ("Rain today to now", 15),
    ("Rain yest ≥cutoff", 15), ("Rule", 9), ("Ground wet", 11),
    ("ok temp", 8), ("ok rain", 8), ("ok cloud", 9), ("ok gust", 8),
    ("ok dry", 8), ("daylight", 9), ("FLYABLE", 9), ("Run h", 8),
]
for j, (name, w) in enumerate(cols, start=1):
    c = hs.cell(1, j, name)
    c.font = Font(FONT, bold=True, size=9, color="FFFFFF")
    c.fill = HDR
    c.alignment = Alignment(horizontal="center", wrap_text=True)
    hs.column_dimensions[get_column_letter(j)].width = w
hs.freeze_panes = "A2"

n = len(hourly)
last = n + 1
RF, RB, RC = f"$F$2:$F${last}", f"$B$2:$B${last}", f"$C$2:$C${last}"

for i, h in enumerate(hourly):
    r = i + 2
    sr, ss = sun.get(h["t"].date(), (None, None))
    hs.cell(r, 1, h["t"]).number_format = "dd.mm.yyyy hh:mm"
    hs.cell(r, 2, h["t"].date()).number_format = "dd.mm.yyyy"
    hs.cell(r, 3, h["t"].hour)
    hs.cell(r, 4, h["temp"]).number_format = "0.0"
    hs.cell(r, 5, h["dew"]).number_format = "0.0"
    hs.cell(r, 6, h["prcp"]).number_format = "0.0"
    hs.cell(r, 7, h["lowcc"]).number_format = "0"
    hs.cell(r, 8, h["totcc"]).number_format = "0"
    hs.cell(r, 9, h["gust"]).number_format = "0.0"
    hs.cell(r, 10, h["wspd"]).number_format = "0.0"
    hs.cell(r, 11, sr).number_format = "dd.mm hh:mm"
    hs.cell(r, 12, ss).number_format = "dd.mm hh:mm"
    hs.cell(r, 13, f"=IF(G{r}>={CCGATE},ROUND(MAX(0,125*(D{r}-E{r})),0),99999)"
            ).number_format = "0"
    # September: rain in the preceding N hours, N read from Settings so the
    # cell is actually live rather than decorative.
    hs.cell(r, 14, f'=SUMIFS({RF},$A$2:$A${last},">="&A{r}-{DRYSEP}/24,'
                   f'$A$2:$A${last},"<"&A{r})').number_format = "0.0"
    # October, same day: any rain already fallen today.
    hs.cell(r, 15, f'=SUMIFS({RF},{RB},B{r},{RC},"<="&C{r})'
            ).number_format = "0.0"
    # October, carry-over: rain yesterday at or after the cutoff hour.
    hs.cell(r, 16, f'=SUMIFS({RF},{RB},B{r}-1,{RC},">="&{OCTCUT})'
            ).number_format = "0.0"
    hs.cell(r, 17, f'=IF(MONTH(B{r})>=10,"Oct","Sep")')
    hs.cell(r, 18, f'=IF(Q{r}="Oct",IF(OR(O{r}>0,P{r}>0),1,0),'
                   f"IF(N{r}>0,1,0))")
    hs.cell(r, 19, f"=IF(D{r}>={MINT},1,0)")
    hs.cell(r, 20, f"=IF(F{r}<={MAXP},1,0)")
    hs.cell(r, 21, f"=IF(M{r}>={MINCB},1,0)")
    hs.cell(r, 22, f"=IF(I{r}<={MAXG},1,0)")
    hs.cell(r, 23, f"=IF(R{r}=0,1,0)")
    hs.cell(r, 24, f'=IF({DAYONLY}<>"yes",1,'
                   f"IF(AND(A{r}>=K{r},A{r}<=L{r}),1,0))")
    hs.cell(r, 25, f"=IF(SUM(S{r}:X{r})=6,1,0)")
    hs.cell(r, 26, f"=IF(Y{r}=1,{'Z'+str(r-1) if r > 2 else '0'}+1,0)")
    for j in range(1, 27):
        hs.cell(r, j).font = Font(FONT, size=9)
print(f"hourly rows written: 2..{last}")

HD, HOK, HRUN = f"Hourly!$B$2:$B${last}", f"Hourly!$Y$2:$Y${last}", f"Hourly!$Z$2:$Z${last}"
HT, HP, HCB = f"Hourly!$D$2:$D${last}", f"Hourly!$F$2:$F${last}", f"Hourly!$M$2:$M${last}"
HG, HLIGHT = f"Hourly!$I$2:$I${last}", f"Hourly!$X$2:$X${last}"
HOKCB, HOKG = f"Hourly!$U$2:$U${last}", f"Hourly!$V$2:$V${last}"
HOKR, HOKDRY = f"Hourly!$T$2:$T${last}", f"Hourly!$W$2:$W${last}"

# ======================================================================== Days
ds = wb.create_sheet("Days", 1)
ds.sheet_view.showGridLines = False
ds["A1"] = "Flight availability — Limbaži (57.514 N, 24.719 E), 1 Sep – 1 Nov 2026"
ds["A1"].font = Font(FONT, size=14, bold=True, color="1F3864")
ds["A2"] = ("Verdict = at least N consecutive daylight hours meeting every criterion. "
            "Thresholds are on the Settings sheet; change one and this re-decides. "
            "Cloud base is DERIVED, and conservative — see Notes.")
ds["A2"].font = Font(FONT, size=9, italic=True, color="595959")

groups = [("", 5), ("Usable time", 3), ("Conditions that day", 6),
          ("Daylight hours blocked by", 4)]
dcols = [
    ("Date", 11), ("Day", 5), ("Sunrise", 8), ("Sunset", 8), ("Data", 9),
    ("Longest run (h)", 9), ("Total (h)", 8), ("VERDICT", 11),
    ("Min °C", 8), ("Max °C", 8), ("Rain mm", 9), ("Rain h", 8),
    ("Min cloud base m", 11), ("Max gust m/s", 10),
    ("cloud", 8), ("gust", 8), ("rain", 8), ("wet ground", 10),
]
col = 1
for title, span in groups:
    if title:
        ds.merge_cells(start_row=3, start_column=col, end_row=3,
                       end_column=col + span - 1)
        c = ds.cell(3, col, title)
        c.font = Font(FONT, bold=True, size=9, color="1F3864")
        c.fill = SUB
        c.alignment = Alignment(horizontal="center")
    col += span
for j, (name, w) in enumerate(dcols, start=1):
    c = ds.cell(4, j, name)
    c.font = Font(FONT, bold=True, size=9, color="FFFFFF")
    c.fill = HDR
    c.alignment = Alignment(horizontal="center", wrap_text=True)
    ds.column_dimensions[get_column_letter(j)].width = w
ds.freeze_panes = "A5"

day, r = D0, 5
while day <= D1:
    a = f"$A{r}"
    ds.cell(r, 1, day).number_format = "dd.mm.yyyy"
    ds.cell(r, 2, day).number_format = "ddd"
    sr, ss = sun[day]
    ds.cell(r, 3, sr).number_format = "hh:mm"
    ds.cell(r, 4, ss).number_format = "hh:mm"
    ds.cell(r, 5, "forecast" if day >= BUILT else "analysis")
    ds.cell(r, 6, f"=_xlfn.MAXIFS({HRUN},{HD},{a})")
    ds.cell(r, 7, f"=COUNTIFS({HD},{a},{HOK},1)")
    ds.cell(r, 8, f'=IF(F{r}>={MINWIN},"FLYABLE","no")')
    ds.cell(r, 9, f"=_xlfn.MINIFS({HT},{HD},{a})").number_format = "0.0"
    ds.cell(r, 10, f"=_xlfn.MAXIFS({HT},{HD},{a})").number_format = "0.0"
    ds.cell(r, 11, f"=SUMIFS({HP},{HD},{a})").number_format = "0.0"
    ds.cell(r, 12, f'=COUNTIFS({HD},{a},{HP},">0")')
    ds.cell(r, 13, f'=IFERROR(IF(_xlfn.MINIFS({HCB},{HD},{a},{HLIGHT},1)>=99999,'
                   f'"none",_xlfn.MINIFS({HCB},{HD},{a},{HLIGHT},1)),"")'
            ).number_format = "0"
    ds.cell(r, 14, f"=_xlfn.MAXIFS({HG},{HD},{a})").number_format = "0.0"
    ds.cell(r, 15, f"=COUNTIFS({HD},{a},{HLIGHT},1,{HOKCB},0)")
    ds.cell(r, 16, f"=COUNTIFS({HD},{a},{HLIGHT},1,{HOKG},0)")
    ds.cell(r, 17, f"=COUNTIFS({HD},{a},{HLIGHT},1,{HOKR},0)")
    ds.cell(r, 18, f"=COUNTIFS({HD},{a},{HLIGHT},1,{HOKDRY},0)")
    for j in range(1, 19):
        c = ds.cell(r, j)
        c.font = Font(FONT, size=9)
        c.border = BOX
        c.alignment = Alignment(horizontal="center")
    ds.cell(r, 8).font = Font(FONT, size=9, bold=True)
    if day >= BUILT:
        ds.cell(r, 5).font = Font(FONT, size=9, italic=True, color="C00000")
    day += dt.timedelta(days=1)
    r += 1
dlast = r - 1

ds.conditional_formatting.add(
    f"A5:R{dlast}", FormulaRule(formula=['$H5="FLYABLE"'], fill=GOOD))
ds.conditional_formatting.add(
    f"H5:H{dlast}", FormulaRule(formula=['$H5="no"'], fill=BAD))

sr0 = dlast + 2
ds.cell(sr0, 1, "Summary").font = Font(FONT, bold=True, size=11, color="1F3864")
summary = [
    ("Days in range", f"=COUNT($A$5:$A${dlast})"),
    ("FLYABLE days", f'=COUNTIF($H$5:$H${dlast},"FLYABLE")'),
    ("Share flyable", f'=COUNTIF($H$5:$H${dlast},"FLYABLE")/COUNT($A$5:$A${dlast})'),
    ("September flyable",
     f'=SUMPRODUCT((MONTH($A$5:$A${dlast})=9)*($H$5:$H${dlast}="FLYABLE"))'),
    ("September days", f"=SUMPRODUCT((MONTH($A$5:$A${dlast})=9)*1)"),
    ("October flyable",
     f'=SUMPRODUCT((MONTH($A$5:$A${dlast})=10)*($H$5:$H${dlast}="FLYABLE"))'),
    ("October days", f"=SUMPRODUCT((MONTH($A$5:$A${dlast})=10)*1)"),
    ("Best single day (h)", f"=MAX($F$5:$F${dlast})"),
    ("Mean longest run (h)", f"=AVERAGE($F$5:$F${dlast})"),
    ("Rows still forecast", f'=COUNTIF($E$5:$E${dlast},"forecast")'),
]
for k, (lab, f_) in enumerate(summary, start=sr0 + 1):
    ds.cell(k, 1, lab).font = Font(FONT, size=9)
    c = ds.cell(k, 3, f_)
    c.font = Font(FONT, size=9, bold=True)
    c.number_format = ("0.0%" if "Share" in lab
                       else "0.0" if "Mean" in lab else "0")

# ======================================================================= Notes
ns = wb.create_sheet("Notes")
ns.sheet_view.showGridLines = False
ns.column_dimensions["A"].width = 118
notes = [
    ("Limbaži flight availability, 1 Sep – 1 Nov 2026", "h1"),
    ("Built 30.10.2026. The window is almost entirely in the past, so this is "
     "a record of what the weather did rather than a forecast. The two "
     "exceptions are 31.10 and 1.11, which had not happened yet: the 'Data' "
     "column marks them 'forecast' in red and their verdicts are provisional. "
     "Every other row is model analysis of weather that occurred.", "p"),
    ("What the verdict means", "h2"),
    ("FLYABLE = at least 3 CONSECUTIVE hours, between sunrise and sunset, in "
     "which all five criteria held at once. Consecutive, not a daily total — "
     "three scattered single hours do not make a sortie. The 'Total (h)' "
     "column shows the daily total separately so you can see the difference.", "p"),
    ("Wet ground — two different rules", "h2"),
    ("September: the ground is wet for 3 h after rain, then usable again the "
     "same day.", "p"),
    ("October: rain wets the ground for the REST of that day — there is no "
     "same-day recovery. Whether it is still wet the following day depends on "
     "when the rain stopped: rain that finished before 15:00 dries overnight "
     "and the next day flies; rain at or after 15:00 leaves the ground wet "
     "through the whole of the next day. The cutoff hour is on the Settings "
     "sheet. The 'Rain today to now' and 'Rain yest ≥cutoff' columns on the "
     "Hourly sheet show the two halves of the test separately.", "p"),
    ("This rule is applied from 1 October onward, so 1 November uses it too. "
     "An October day also inherits the test from 30 September, which is "
     "included in the data.", "p"),
    ("Data source", "h2"),
    ("Hourly values are Open-Meteo's analysis at 57.514 N, 24.719 E — ICON at "
     "2–7 km over Latvia, which is finer than ERA5's 25 km. Temperature, dew "
     "point, rain, low and total cloud, 10 m wind and 10 m gusts, in local "
     "(Europe/Riga) time. Sunrise and sunset are for that exact point.", "p"),
    ("Why the project's own database was not used: wx.observations_h retains "
     "about sixteen days (every station's record starts 14.10.2026), so it "
     "holds none of September. No station within 80 km reports a cloud "
     "ceiling at all.", "p"),
    ("Cloud base is DERIVED — read this before trusting it", "h2"),
    ("No station near Limbaži measures cloud base, so it is computed as the "
     "lifted condensation level, 125 m per °C of temperature/dew-point "
     "spread, and applied as a ceiling ONLY where low cloud is at least 50% "
     "(the meteorological definition of a ceiling: the lowest broken or "
     "overcast layer). Where low cloud is thinner the sheet records 'no "
     "ceiling' — a clear sky has no cloud at its condensation level, and "
     "treating the LCL as a ceiling regardless would have marked almost every "
     "day unflyable, since the period's median LCL is only 150 m.", "p"),
    ("That derivation was checked against 1,139 Riga (EVRA) METAR reports "
     "carrying a real measured ceiling, comparing each report's own ceiling "
     "with its own dew-point spread. The derivation runs LOW by a median of "
     "152 m (mean absolute error 331 m). At the 1000 m gate this sheet uses "
     "it agrees with the measured ceiling 83% of the time, and the "
     "disagreement is almost entirely one-sided: it wrongly BLOCKS 16.9% of "
     "ceiling-limited hours and wrongly CLEARS 0.0%.", "p"),
    ("So: a day marked FLYABLE is reliable. A day marked 'no' on cloud "
     "grounds may actually have been flyable — roughly one ceiling-limited "
     "hour in six. The error leans the safe way, but it means this table "
     "UNDERCOUNTS good days rather than overcounting them.", "p"),
    ("Assumptions I had to make", "h2"),
    ("You gave no number for gusts or temperature. Gust limit is set to "
     "12 m/s (~23 kt) and the temperature floor to 0 °C. Both are on the "
     "Settings sheet: change the cell and every verdict updates. The "
     "temperature floor never actually excluded an hour — the period's "
     "coldest was +0.6 °C — so gusts are the assumption that matters.", "p"),
    ("Minimum cloud base is 1000 m AGL: your 700 m flight altitude plus "
     "300 m (1000 ft) of VFR clearance below cloud. If you do not need that "
     "clearance, set it to 0 on the Settings sheet and the requirement drops "
     "to 700 m.", "p"),
    ("Known limitations", "h2"),
    ("Rain is a modelled hourly total at a single point; a shower that missed "
     "the point by 2 km is not in it, and the October rule turns on whether "
     "any rain fell after 15:00, so a single marginal wet hour late in the "
     "day costs the next day entirely. Gusts from a 2–7 km model are smoother "
     "than a real anemometer — expect true peaks somewhat higher than the "
     "'Max gust' column. 'Wet ground' is inferred purely from rain timing; it "
     "knows nothing about your actual surface, drainage, sun or wind, all of "
     "which matter more than the clock in September.", "p"),
]
r = 1
for text, kind in notes:
    c = ns.cell(r, 1, text)
    if kind == "h1":
        c.font = Font(FONT, size=14, bold=True, color="1F3864")
        ns.row_dimensions[r].height = 22
    elif kind == "h2":
        c.font = Font(FONT, size=11, bold=True, color="1F3864")
        ns.row_dimensions[r].height = 26
    else:
        c.font = Font(FONT, size=9)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ns.row_dimensions[r].height = 14 * (len(text) // 105 + 1)
    r += 1

os.makedirs(os.path.dirname(OUT), exist_ok=True)
wb.save(OUT)
print("saved", OUT, os.path.getsize(OUT), "bytes")
print("days rows 5 ..", dlast)

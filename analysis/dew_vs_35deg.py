import json, datetime as dt, sys
sys.path.insert(0,'/tmp')
import dew as D
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
d=json.load(open('/tmp/riga_year.json'))['hourly']
n=len(d['time']); H=[]
for i in range(n):
    ts=dt.datetime.fromisoformat(d['time'][i]).replace(tzinfo=dt.timezone.utc)
    H.append(dict(ts=ts+dt.timedelta(minutes=30), t2m=d['temperature_2m'][i],
                  td2m=d['dew_point_2m'][i], ws10m=d['wind_speed_10m'][i],
                  cc_total=d['cloud_cover'][i],
                  prcp=d['precipitation'][i+1] if i+1<n else 0.0))
R=D.run(H); LOC=3.0
pts=[]
for i in range(1,len(R)-20):
    if not (R[i]['elev']>0 and R[i-1]['elev']<=0): continue
    if R[i]['water']<=0.02: continue
    seq=R[i:i+20]
    bo=next((k for k,h in enumerate(seq) if h['water']<=0.0), None)
    if bo is None: continue
    hr=lambda t:(t.hour+t.minute/60+LOC)%24
    pts.append((R[i]['ts'].timetuple().tm_yday, hr(seq[bo]['ts']), R[i]['frost']))
fig,ax=plt.subplots(figsize=(11,5.4), dpi=170)
fig.patch.set_facecolor("#fbf3de"); ax.set_facecolor("#fdf9ec")
xs=[];ys=[]
for doy in range(1,366):
    day=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)+dt.timedelta(days=doy-1)
    m=[k for k in range(0,1440,5) if D.solar_elev(day+dt.timedelta(minutes=k))>=35]
    if m: xs.append(doy); ys.append((m[0]/60+LOC)%24)
ax.axvspan(1,xs[0],color="#c62828",alpha=.055)
ax.axvspan(xs[-1],365,color="#c62828",alpha=.055)
ax.plot(xs,ys,color="#c62828",lw=2.4,zorder=4,label="sun first reaches 35° — exists only 26 Mar to 17 Sep")
dw=[(a,b) for a,b,f in pts if not f]; fr=[(a,b) for a,b,f in pts if f]
ax.scatter([a for a,b in dw],[b for a,b in dw],s=26,color="#2e7d32",alpha=.85,
           edgecolor="none",zorder=5,label="modelled dry-out — dew")
ax.scatter([a for a,b in fr],[b for a,b in fr],s=34,color="#5b6bc0",alpha=.95,
           marker="^",edgecolor="none",zorder=5,label="modelled dry-out — frost")
ax.set_ylim(3,21.5); ax.set_xlim(1,365)
ax.set_xticks([1,32,60,91,121,152,182,213,244,274,305,335])
ax.set_xticklabels("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),fontsize=9)
ax.set_yticks(range(4,21,2)); ax.set_yticklabels([f"{h:02d}:00" for h in range(4,21,2)],fontsize=9)
ax.grid(color="#e6dec6",lw=.9); ax.set_axisbelow(True)
for s in ax.spines.values(): s.set_color("#c9c0a4")
ax.set_title("When the ground dries at Riga, against the 35°-elevation rule\n"
             "one year of reanalysis · 102 mornings that started wet and dried",
             fontsize=12.5,color="#333127",loc="left",pad=12)
ax.set_ylabel("local time",fontsize=10,color="#333127")
ax.legend(loc="lower center",fontsize=9,ncol=3,framealpha=.95,
          facecolor="#fdf9ec",edgecolor="#c9c0a4")
for x in (45,330):
    ax.annotate("", xy=(x,16.4), xytext=(x,19.4),
                arrowprops=dict(arrowstyle="->",color="#a33",lw=1.2))
ax.text(187,19.7,"in the shaded halves the sun never gets to 35° at all —\n"
                 "the ground dries anyway, just later in the day",
        fontsize=9.5,color="#a33",ha="center",va="bottom")
plt.tight_layout(); plt.savefig("/tmp/dew_vs_35.png",facecolor=fig.get_facecolor())
print("ok")

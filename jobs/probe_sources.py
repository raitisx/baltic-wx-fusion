"""One-off: fetch a CURRENT image from every radar source available/candidate
— our own composite (R2), SMHI Sweden composite (Gotland covers the Baltic
sea), FMI Finland composite (covers the Gulf + N Estonia) — downscale, and
print each as one base64 line (@@IMG:<name>:<b64>) for eyeballing. Throwaway."""
import base64
import io
import json
import os
import sys

import boto3
import requests
from PIL import Image

OUT = []


def emit(name, img_bytes, max_px=620):
    try:
        im = Image.open(io.BytesIO(img_bytes))
        im.load()
        if max(im.size) > max_px:
            sc = max_px / max(im.size)
            im = im.resize((int(im.width * sc), int(im.height * sc)))
        buf = io.BytesIO()
        im.convert("RGBA").quantize(colors=192).save(buf, "PNG", optimize=True)
        b = buf.getvalue()
        print(f"@@IMG:{name}:{base64.b64encode(b).decode()}")
        OUT.append((name, len(b)))
    except Exception as e:
        print(f"@@ERR:{name}: {type(e).__name__}: {e}")


s = requests.Session()
s.headers["User-Agent"] = "baltic-wx-fusion probe (weather radar survey)"

# --- our current composite, from R2 ---------------------------------------
try:
    s3 = boto3.client("s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
    arch = json.loads(s3.get_object(Bucket=os.environ["R2_BUCKET"],
                                    Key="maps/radar/archive.json")["Body"].read())
    vtag = arch["hours"][max(arch["hours"])]
    body = s3.get_object(Bucket=os.environ["R2_BUCKET"],
                         Key=f"maps/radar/frames3059/{vtag}.png")["Body"].read()
    print(f"ours: frame {vtag}, {len(body)} B")
    emit("ours_" + vtag, body)
except Exception as e:
    print(f"@@ERR:ours: {type(e).__name__}: {e}")

# --- SMHI Sweden composite (open, key-free) --------------------------------
try:
    r = s.get("https://opendata-download-radar.smhi.se/api/version/latest/"
              "area/sweden/product/comp", timeout=60)
    r.raise_for_status()
    files = r.json().get("files", [])
    latest = max(files, key=lambda f: f.get("valid", ""))
    png = next(f for f in latest["formats"] if f["key"] == "png")
    print(f"smhi: valid {latest.get('valid')}, {png['link']}")
    img = s.get(png["link"], timeout=60)
    img.raise_for_status()
    emit("smhi_" + str(latest.get("valid", "x"))[:16].replace(":", ""), img.content)
except Exception as e:
    print(f"@@ERR:smhi: {type(e).__name__}: {e}")

# --- FMI Finland composite via open WMS (key-free) -------------------------
try:
    wms = ("https://openwms.fmi.fi/geoserver/Radar/wms?service=WMS&version=1.3.0"
           "&request=GetMap&layers=Radar:suomi_dbz_eureffin&styles="
           "&crs=EPSG:4326&bbox=57.0,19.0,61.5,29.5"
           "&width=630&height=540&format=image/png&transparent=false")
    img = s.get(wms, timeout=90)
    img.raise_for_status()
    ct = img.headers.get("content-type", "")
    print(f"fmi: {ct}, {len(img.content)} B")
    if "image" in ct:
        emit("fmi_suomi_dbz", img.content)
    else:
        print("@@ERR:fmi: non-image response: " + img.content[:200].decode("utf-8", "replace"))
except Exception as e:
    print(f"@@ERR:fmi: {type(e).__name__}: {e}")

print("SUMMARY:", OUT)
print("DONE")

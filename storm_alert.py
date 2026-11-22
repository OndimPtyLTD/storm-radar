"""Ping your phone (via ntfy) when a strong or severe thunderstorm is due within 2 hours.

Runs on a schedule in GitHub Actions. Uses the same risk model as the Thunderwatch app:
Open-Meteo instability, rain chance and thunder codes on a 7x7 grid around your spot,
with storm drift from the 850/700/500 hPa winds.
"""
import json, math, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

LAT = float(os.getenv("LAT", "-29.87"))
LON = float(os.getenv("LON", "30.89"))
NAME = os.getenv("PLACE", "Queensburgh")
TOPIC = os.getenv("NTFY_TOPIC", "")
APP_URL = os.getenv("APP_URL", "")
TEST = os.getenv("TEST", "false").lower() == "true"
STATE = "state/last_alert.json"

THRESHOLD = 0.70        # "Very high" and above (estimated category, not a probability)
WINDOW_H = 2.0          # look-ahead
COOLDOWN_H = 3.0        # no repeat pings for the same level within this
N, STEP = 7, 0.45
LEVELS = [(0.85, "Extreme"), (0.70, "Very high"), (0.50, "High"), (0.30, "Moderate"), (0.15, "Low")]
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def level(r):
    return next((n for m, n in LEVELS if r >= m), "None")


def risk(code, cape, li, pp):
    """Estimated thunderstorm potential 0..1 (heuristic, not a calibrated probability).
    Returns None when CAPE is missing, so a data gap is never treated as zero risk."""
    code_s = 0.92 if code in (96, 99) else 0.78 if code == 95 else 0
    if cape is None:
        return code_s or None
    sc = min(max((cape - 150) / 2350, 0), 1)
    sl = sc if li is None else min(max(-li / 6, 0), 1)
    sp = 0.5 if pp is None else pp / 100
    showery = 0.2 if code in (80, 81, 82) else 0
    env = (0.45 * sc + 0.25 * sl) * (0.4 + 0.6 * sp) + 0.3 * sp * sc
    return min(max(code_s, showery, env), 1)


def motion(H, i):
    u = v = n = 0
    for p in ("850", "700", "500"):
        s, d = H.get(f"wind_speed_{p}hPa", [None])[i], H.get(f"wind_direction_{p}hPa", [None])[i]
        if s is None or d is None:
            continue
        r = math.radians(d); u += -s * math.sin(r); v += -s * math.cos(r); n += 1
    if not n:
        return 0.0, 0.0
    u /= n; v /= n
    return (math.degrees(math.atan2(u, v)) + 360) % 360, math.hypot(u, v)


def haversine(a, b):
    r = math.radians
    s = math.sin(r(b[0] - a[0]) / 2) ** 2 + math.cos(r(a[0])) * math.cos(r(b[0])) * math.sin(r(b[1] - a[1]) / 2) ** 2
    return 12742 * math.asin(math.sqrt(s))


def bearing(a, b):
    r = math.radians
    y = math.sin(r(b[1] - a[1])) * math.cos(r(b[0]))
    x = math.cos(r(a[0])) * math.sin(r(b[0])) - math.sin(r(a[0])) * math.cos(r(b[0])) * math.cos(r(b[1] - a[1]))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def ang(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def fetch():
    step_lon = STEP / math.cos(math.radians(LAT))
    grid = [(round(LAT + (i - 3) * STEP, 3), round(LON + (j - 3) * step_lon, 3)) for i in range(N) for j in range(N)]
    q = {
        "latitude": ",".join(str(g[0]) for g in grid),
        "longitude": ",".join(str(g[1]) for g in grid),
        "hourly": "weather_code,cape,lifted_index,precipitation_probability,wind_gusts_10m,"
                  "wind_speed_850hPa,wind_direction_850hPa,wind_speed_700hPa,wind_direction_700hPa,"
                  "wind_speed_500hPa,wind_direction_500hPa",
        "forecast_hours": 3, "timeformat": "unixtime", "timezone": "auto", "wind_speed_unit": "kmh",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
            return grid, (data if isinstance(data, list) else [data])
        except Exception as e:
            print(f"Fetch failed ({e}), retrying"); time.sleep(5 * (attempt + 1))
    sys.exit("Open-Meteo unreachable")


def assess(grid, data):
    now = time.time(); here = (LAT, LON); mid = len(grid) // 2
    off = data[0].get("utc_offset_seconds", 7200)
    hhmm = lambda t: datetime.fromtimestamp(t + off, timezone.utc).strftime("%H:%M")
    hits, missing = [], 0
    for k, d in enumerate(data):
        H = d.get("hourly") or {}
        times = H.get("time") or []
        col = lambda key: H.get(key) or [None] * len(times)
        codes, capes, lis, pps, gusts = col("weather_code"), col("cape"), col("lifted_index"), col("precipitation_probability"), col("wind_gusts_10m")
        for i, t in enumerate(times):
            if t > now + WINDOW_H * 3600:
                continue
            r = risk(codes[i], capes[i], lis[i], pps[i])
            hail = codes[i] in (96, 99)
            if r is None:
                missing += 1
                if not hail:
                    continue
                r = 0.92
            if r < THRESHOLD and not hail:
                continue
            gust = gusts[i] or 0
            if k == mid:
                hits.append({"eta": max(t, now), "r": r, "hail": hail, "gust": gust,
                             "text": f"{level(r)} thunderstorm risk (forecast estimate) over {NAME} from about {hhmm(max(t, now))}"})
                continue
            b, spd = motion(H, i) if H.get("wind_speed_500hPa") else (0, 0)
            if spd < 8 or ang(b, bearing(grid[k], here)) > 35:
                continue
            dist = haversine(grid[k], here)
            eta = max(t, now) + dist / spd * 3600
            if eta - now <= WINDOW_H * 3600:
                frm = DIRS[round(bearing(here, grid[k]) / 22.5) % 16]
                hits.append({"eta": eta, "r": r, "hail": hail, "gust": gust,
                             "text": f"Forecast: {level(r).lower()} thunderstorm risk area {round(dist)} km {frm}, "
                                     f"drifting your way at {round(spd)} km/h, could arrive around {hhmm(eta)} "
                                     f"(model estimate, not a tracked storm)"})
    if missing:
        print(f"Warning: {missing} grid-hour values had no instability data and were skipped (not treated as zero risk).")
    hits.sort(key=lambda h: (h["eta"], -h["r"]))
    return hits


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def ping(title, body, priority="urgent"):
    if not TOPIC:
        sys.exit("NTFY_TOPIC secret is not set")
    headers = {"Title": title, "Priority": priority, "Tags": "zap,warning"}
    if APP_URL:
        headers["Click"] = APP_URL
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=body.encode(), headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=20).read()
    print("Pinged:", title, "|", body)


def main():
    if TEST:
        ping("Thunderwatch test", f"Alerts for {NAME} are set up. You’ll get a ping like this when forecast thunderstorm risk is very high within 2 hours.", "default")
        return
    grid, data = fetch()
    hits = assess(grid, data)
    if not hits:
        print("No strong storms within 2 hours."); return
    worst = max(hits, key=lambda h: h["r"])
    lvl = level(worst["r"])
    st = load_state()
    rank = {"Very high": 1, "Extreme": 2}
    if st and time.time() - st.get("t", 0) < COOLDOWN_H * 3600 and rank.get(lvl, 0) <= rank.get(st.get("level"), 0):
        print(f"Already alerted ({st.get('level')}) recently; skipping."); return
    lines = [hits[0]["text"] + "."]
    extras = []
    if any(h["hail"] for h in hits): extras.append("hail possible")
    g = max(h["gust"] for h in hits)
    if g >= 60: extras.append(f"gusts to {round(g)} km/h")
    if extras: lines.append(", ".join(extras).capitalize() + ".")
    ping(f"{lvl} thunderstorm risk within 2 hours", " ".join(lines))
    os.makedirs("state", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"t": time.time(), "level": lvl}, f)


if __name__ == "__main__":
    main()

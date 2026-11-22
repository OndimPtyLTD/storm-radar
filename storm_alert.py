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

THRESHOLD = 0.70        # Strong and above
WINDOW_H = 2.0          # look-ahead
COOLDOWN_H = 3.0        # no repeat pings for the same level within this
N, STEP = 7, 0.45
LEVELS = [(0.85, "Severe"), (0.70, "Strong"), (0.50, "Likely"), (0.30, "Possible"), (0.15, "Low")]
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def level(r):
    return next((n for m, n in LEVELS if r >= m), "None")


def risk(code, cape, li, pp):
    cape = cape or 0; li = 2 if li is None else li; pp = pp or 0
    code_s = 0.92 if code in (96, 99) else 0.78 if code == 95 else 0.2 if code in (80, 81, 82) else 0
    sc = min(max((cape - 150) / 2350, 0), 1); sl = min(max(-li / 6, 0), 1); sp = pp / 100
    env = (0.45 * sc + 0.25 * sl) * (0.4 + 0.6 * sp) + 0.3 * sp * sc
    return min(max(code_s, env), 1)


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
    hits = []
    for k, d in enumerate(data):
        H = d["hourly"]
        for i, t in enumerate(H["time"]):
            if t > now + WINDOW_H * 3600:
                continue
            r = risk(H["weather_code"][i], H["cape"][i], H["lifted_index"][i], H["precipitation_probability"][i])
            hail = H["weather_code"][i] in (96, 99)
            if r < THRESHOLD and not hail:
                continue
            gust = H["wind_gusts_10m"][i] or 0
            if k == mid:
                hits.append({"eta": max(t, now), "r": r, "hail": hail, "gust": gust,
                             "text": f"{level(r)} storm risk over {NAME} from about {hhmm(max(t, now))}"})
                continue
            b, spd = motion(H, i)
            if spd < 8 or ang(b, bearing(grid[k], here)) > 35:
                continue
            dist = haversine(grid[k], here)
            eta = max(t, now) + dist / spd * 3600
            if eta - now <= WINDOW_H * 3600:
                frm = DIRS[round(bearing(here, grid[k]) / 22.5) % 16]
                hits.append({"eta": eta, "r": r, "hail": hail, "gust": gust,
                             "text": f"{level(r)} cell {round(dist)} km {frm} moving your way at {round(spd)} km/h, "
                                     f"arriving around {hhmm(eta)}"})
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
        ping("Thunderwatch test", f"Alerts for {NAME} are set up. You’ll get a ping like this when a strong storm is due within 2 hours.", "default")
        return
    grid, data = fetch()
    hits = assess(grid, data)
    if not hits:
        print("No strong storms within 2 hours."); return
    worst = max(hits, key=lambda h: h["r"])
    lvl = level(worst["r"])
    st = load_state()
    rank = {"Strong": 1, "Severe": 2}
    if st and time.time() - st.get("t", 0) < COOLDOWN_H * 3600 and rank.get(lvl, 0) <= rank.get(st.get("level"), 0):
        print(f"Already alerted ({st.get('level')}) recently; skipping."); return
    lines = [hits[0]["text"] + "."]
    extras = []
    if any(h["hail"] for h in hits): extras.append("hail possible")
    g = max(h["gust"] for h in hits)
    if g >= 60: extras.append(f"gusts to {round(g)} km/h")
    if extras: lines.append(", ".join(extras).capitalize() + ".")
    ping(f"{lvl} storm within 2 hours", " ".join(lines))
    os.makedirs("state", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"t": time.time(), "level": lvl}, f)


if __name__ == "__main__":
    main()

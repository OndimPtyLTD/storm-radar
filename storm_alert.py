"""Thunderwatch South Africa storm alerts.

Every run checks one national forecast grid (1 degree, about 110 km) covering South Africa
and pings, through ntfy, anyone subscribed to a town whose forecast thunderstorm risk within
the next 2 hours is "Extreme", or "Very high" with the model also forecasting thunder, or
where hail is forecast. (Stricter than the app's colours, to limit false alarms.)

- Public town topics: thunderwatch-za-<town slug> (listed in towns.json); anyone can subscribe.
- The owner's private topic (NTFY_TOPIC secret) gets the alerts for OWNER_TOWN.

Risk categories are estimates from forecast instability, rain chance and model thunder codes.
They are not calibrated probabilities and not observed storms; messages say so.
"""
import json, math, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone

TOPIC = os.getenv("NTFY_TOPIC", "")
OWNER_TOWN = os.getenv("OWNER_TOWN", "durban")
APP_URL = os.getenv("APP_URL", "https://ondimptyltd.github.io/storm-radar/")
TEST = os.getenv("TEST", "false").lower() == "true"
PUBLIC_PREFIX = "thunderwatch-za-"
STATE = "state/alerts.json"
ACTIVE = "alerts/active.json"   # public list the website shows; only rewritten when it changes

THRESHOLD = 0.70        # "Very high" (needs a model thunder code too); 0.85+ "Extreme" alerts alone
WINDOW_H = 2.0          # look-ahead
COOLDOWN_H = 3.0        # no repeat pings for the same town and level within this
OVER_KM = 80            # grid cells this close count as "over the town"
REACH_KM = 260          # how far away an approaching high-risk area is considered
STEP, RADIUS_KM = 1.0, 180
LEVELS = [(0.85, "Extreme"), (0.70, "Very high"), (0.50, "High"), (0.30, "Moderate"), (0.15, "Low")]
DIRS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
SAFETY = "If you hear thunder, go indoors and stay away from floodwater. Follow SA Weather Service warnings."

TOWNS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "towns.json")))


def level(r):
    return next((n for m, n in LEVELS if r >= m), "Minimal")


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


def dist_km(a, b):
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


def national_grid():
    """1-degree points within RADIUS_KM of any listed town (keeps the grid on South Africa)."""
    pts, lat = [], -22.0
    while lat >= -35.0:
        lon = 16.5
        while lon <= 33.0:
            if min(dist_km((lat, lon), (t["lat"], t["lon"])) for t in TOWNS) <= RADIUS_KM:
                pts.append((round(lat, 2), round(lon, 2)))
            lon += STEP
        lat -= STEP
    return pts


def fetch(grid):
    q = {
        "latitude": ",".join(str(g[0]) for g in grid),
        "longitude": ",".join(str(g[1]) for g in grid),
        "hourly": "weather_code,cape,lifted_index,precipitation_probability,wind_gusts_10m,"
                  "wind_speed_850hPa,wind_direction_850hPa,wind_speed_500hPa,wind_direction_500hPa",
        "forecast_hours": 3, "timeformat": "unixtime", "timezone": "Africa/Johannesburg", "wind_speed_unit": "kmh",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.load(r)
            return data if isinstance(data, list) else [data]
        except Exception as e:
            print(f"Fetch failed ({e}), retrying")
            time.sleep(10 * (attempt + 1))
    sys.exit("Open-Meteo unreachable: no alerts could be checked this run")


def cells(grid, data, now):
    """Per grid point and forecast hour: estimated risk, hail flag, gust and storm drift."""
    out, missing = [], 0
    for k, d in enumerate(data):
        H = d.get("hourly") or {}
        times = H.get("time") or []
        col = lambda key: H.get(key) or [None] * len(times)
        codes, capes, lis, pps, gusts = col("weather_code"), col("cape"), col("lifted_index"), col("precipitation_probability"), col("wind_gusts_10m")
        s85, d85, s50, d50 = col("wind_speed_850hPa"), col("wind_direction_850hPa"), col("wind_speed_500hPa"), col("wind_direction_500hPa")
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
            u = v = n = 0
            for s, dd in ((s85[i], d85[i]), (s50[i], d50[i])):
                if s is None or dd is None:
                    continue
                rr = math.radians(dd); u += -s * math.sin(rr); v += -s * math.cos(rr); n += 1
            drift = ((math.degrees(math.atan2(u / n, v / n)) + 360) % 360, math.hypot(u / n, v / n)) if n else (0, 0)
            out.append({"pt": grid[k], "t": max(t, now), "r": r, "hail": hail, "tcode": codes[i] in (95, 96, 99),
                        "gust": gusts[i] or 0, "drift": drift})
    if missing:
        print(f"Warning: {missing} grid-hour values had no instability data and were skipped (not treated as zero risk).")
    return out


def assess_town(town, cs, now, hhmm):
    here = (town["lat"], town["lon"])
    hits = []
    for c in cs:
        if not (c["hail"] or c["r"] >= 0.85 or (c["r"] >= THRESHOLD and c["tcode"])):
            continue
        d = dist_km(c["pt"], here)
        if d <= OVER_KM:
            hits.append({"eta": c["t"], "c": c,
                         "text": f"{level(c['r'])} thunderstorm risk over {town['name']} from about {hhmm(c['t'])} (forecast estimate)"})
            continue
        b, spd = c["drift"]
        if d > REACH_KM or spd < 8 or ang(b, bearing(c["pt"], here)) > 35:
            continue
        eta = c["t"] + d / spd * 3600
        if eta - now <= WINDOW_H * 3600:
            frm = DIRS[round(bearing(here, c["pt"]) / 22.5) % 16]
            hits.append({"eta": eta, "c": c,
                         "text": f"Forecast: {level(c['r']).lower()} thunderstorm risk area about {round(d)} km {frm} of {town['name']}, "
                                 f"drifting your way at {round(spd)} km/h, could arrive around {hhmm(eta)} (model estimate, not a tracked storm)"})
    hits.sort(key=lambda h: h["eta"])
    return hits


def ping(topic, title, body, click, priority="urgent"):
    headers = {"Title": title, "Priority": priority, "Tags": "zap,warning", "Click": click}
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode(), headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20).read()
        print("Pinged", topic, "|", title)
    except Exception as e:
        print("Ping failed", topic, e)


def town_link(t):
    return APP_URL + "?" + urllib.parse.urlencode({"lat": t["lat"], "lon": t["lon"], "name": t["name"]})


def main():
    owner = next((t for t in TOWNS if t["slug"] == OWNER_TOWN), None)
    if TEST:
        if not TOPIC:
            sys.exit("NTFY_TOPIC secret is not set")
        name = owner["name"] if owner else OWNER_TOWN
        ping(TOPIC, "Thunderwatch test", f"Alerts for {name} are set up. You’ll get a ping like this when forecast thunderstorm risk is very high within 2 hours.", APP_URL, "default")
        return
    now = time.time()
    grid = national_grid()
    data = fetch(grid)
    off = data[0].get("utc_offset_seconds", 7200)
    hhmm = lambda t: datetime.fromtimestamp(t + off, timezone.utc).strftime("%H:%M")
    cs = cells(grid, data, now)
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}
    try:
        active = {a["slug"]: a for a in json.load(open(ACTIVE)).get("alerts", [])}
    except Exception:
        active = {}
    before = json.dumps(sorted(active.values(), key=lambda a: a["slug"]), sort_keys=True)
    active = {k: v for k, v in active.items() if v.get("until", 0) > now}   # drop expired alerts
    rank = {"Very high": 1, "Extreme": 2}
    sent = 0
    for town in TOWNS:
        hits = assess_town(town, cs, now, hhmm)
        if not hits:
            continue
        lvl = level(max(h["c"]["r"] for h in hits))
        until = round(max(h["eta"] for h in hits) + 3600)
        extras = []
        if any(h["c"]["hail"] for h in hits): extras.append("hail possible")
        g = max(h["c"]["gust"] for h in hits)
        if g >= 60: extras.append(f"gusts to {round(g)} km/h")
        body = hits[0]["text"] + "." + (" " + ", ".join(extras).capitalize() + "." if extras else "") + " " + SAFETY
        title = f"{town['name']}: {lvl} thunderstorm risk within 2 hours"
        entry = {"slug": town["slug"], "name": town["name"], "province": town["province"], "lat": town["lat"], "lon": town["lon"],
                 "level": lvl, "title": title, "body": body, "issued": round(now), "until": until}
        st = state.get(town["slug"], {})
        if st and now - st.get("t", 0) < COOLDOWN_H * 3600 and rank.get(lvl, 0) <= rank.get(st.get("level"), 0):
            print(f"{town['name']}: already alerted ({st.get('level')}) recently; skipping.")
            if town["slug"] in active:
                active[town["slug"]]["until"] = max(active[town["slug"]]["until"], until)
            else:
                active[town["slug"]] = {**entry, "issued": round(st.get("t", now))}
            continue
        ping(PUBLIC_PREFIX + town["slug"], title, body, town_link(town))
        active[town["slug"]] = entry
        if TOPIC and owner and town["slug"] == owner["slug"]:
            ping(TOPIC, title, body, town_link(town))
        state[town["slug"]] = {"t": now, "level": lvl}
        sent += 1
    print(f"Checked {len(TOWNS)} towns on {len(grid)} grid points; {sent} alert(s) sent.")
    os.makedirs("state", exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(state, f)
    if json.dumps(sorted(active.values(), key=lambda a: a["slug"]), sort_keys=True) != before:
        os.makedirs("alerts", exist_ok=True)
        with open(ACTIVE, "w") as f:
            json.dump({"updated": round(now), "alerts": sorted(active.values(), key=lambda a: -a["issued"])}, f, indent=1)
        print(f"Active alert list updated: {len(active)} active.")


if __name__ == "__main__":
    main()

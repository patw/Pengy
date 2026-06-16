#!/usr/bin/env python3
"""Fetch weather from tomorrow.io with rate-limit-aware throttling (3/s, 25/hr, 500/day)."""
import argparse, fcntl, json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

GAP = 0.35
LOCK = Path("/tmp/tomorrow_io_rate.lock")
USAGE = Path("/tmp/tomorrow_io_usage.json")

def _load():
    if USAGE.exists():
        try: return json.loads(USAGE.read_text())
        except: pass
    return {"last":0, "hc":0, "hs":0, "dc":0, "ds":0}

def _save(u):
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps(u))

def _wait(u):
    now = time.time()
    gap = now - u["last"]
    if gap < GAP: time.sleep(GAP - gap); now = time.time()
    he = now - u["hs"]
    if he >= 3600: u["hc"]=0; u["hs"]=now
    elif u["hc"] >= 25:
        s = 3600 - he + 1
        print(f"[throttle] hourly limit — sleep {s:.0f}s", file=sys.stderr)
        time.sleep(s); now = time.time(); u["hc"]=0; u["hs"]=now
    de = now - u["ds"]
    if de >= 86400: u["dc"]=0; u["ds"]=now
    elif u["dc"] >= 500:
        s = 86400 - de + 1
        print(f"[throttle] daily limit — sleep {s:.0f}s", file=sys.stderr)
        time.sleep(s); now = time.time(); u["dc"]=0; u["ds"]=now

def _record(u):
    now = time.time()
    u["last"]=now; u["hc"]+=1; u["dc"]+=1
    if u["hs"]==0: u["hs"]=now
    if u["ds"]==0: u["ds"]=now
    _save(u)

def api_call(url, params, retries=3):
    fd = os.open(LOCK, os.O_CREAT|os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        u = _load()
        _wait(u)
        for a in range(retries):
            try: r = requests.get(url, params=params, timeout=30)
            except: print("error: network failure", file=sys.stderr); sys.exit(1)
            if r.status_code == 429:
                s = int(r.headers.get("Retry-After", 2**a))
                print(f"[retry] 429 — wait {s}s ({a+1}/{retries})", file=sys.stderr)
                time.sleep(s); continue
            if r.status_code == 403:
                print("error: 403 — check API key, plan limits, or data fields", file=sys.stderr); sys.exit(1)
            r.raise_for_status()
            _record(u)
            return r
        print("error: exhausted retries", file=sys.stderr); sys.exit(1)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

def _read_secrets():
    """Read key=value pairs from ~/.secrets file."""
    secrets = {}
    secret_file = Path.home() / ".secrets"
    if secret_file.exists():
        for line in secret_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip()
    return secrets

def main():
    p = argparse.ArgumentParser()
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--timezone", default="America/Toronto")
    a = p.parse_args()
    secrets = _read_secrets()
    key = os.environ.get("TOMORROW_IO_KEY", secrets.get("TOMORROW_IO_KEY", ""))
    if not key:
        print("error: TOMORROW_IO_KEY not found in env or ~/.secrets", file=sys.stderr); sys.exit(1)
    fields = ["precipitationIntensity","precipitationType","windSpeed","windGust",
              "windDirection","temperature","temperatureApparent","cloudCover",
              "cloudBase","cloudCeiling","weatherCode"]
    now = datetime.now(timezone.utc)
    if a.days > 5: print(f"[warn] max 5 days (requested {a.days})", file=sys.stderr)
    end = (now + timedelta(days=max(1,min(a.days,5)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"apikey":key,"location":f"{a.lat},{a.lon}","fields":",".join(fields),
              "units":"metric","timesteps":"current,1h,1d",
              "startTime":now.strftime("%Y-%m-%dT%H:%M:%SZ"),"endTime":end,
              "timezone":a.timezone}
    r = api_call("https://api.tomorrow.io/v4/timelines", params)
    print(json.dumps(r.json(), indent=2))

if __name__=="__main__": main()

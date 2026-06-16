# Weather Skill

Fetches weather from tomorrow.io. Rate-limit throttling (file-lock in /tmp) is automatic.

```
python get_weather_by_location.py <lat> <lon> [--days N] [--timezone TZ]
```

| Arg | Default | Desc |
|-----|---------|------|
| lat | required | Decimal lat |
| lon | required | Decimal lon |
| --days | 1 | Forecast days (max 5) |
| --timezone | America/Toronto | IANA tz |

**Limits (auto-handled):** 3 req/s (350ms gap), 25/hr, 500/day. 429 → exp backoff.  
**API key:** Read from `TOMORROW_IO_KEY` env var or `~/.secrets` file (key: `TOMORROW_IO_KEY`).

**Output:** JSON to stdout with `current`, `1h`, `1d` timesteps: temperature, temperatureApparent, precipitationIntensity, precipitationType, windSpeed, windGust, windDirection, cloudCover, cloudBase, cloudCeiling, weatherCode.

**Example:** `python get_weather_by_location.py 44.446 -79.728 --days 7`

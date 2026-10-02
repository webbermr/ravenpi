# Aircraft Hex Lookup via hexdb.io

A portable reference for enriching ADS-B / aircraft data with **current
registration, owner, and type** from an aircraft's ICAO 24-bit hex address.

> TL;DR: `GET https://hexdb.io/api/v1/aircraft/{HEX}` — free, no API key, open
> CORS, JSON response. Cache results, use a short timeout, and degrade
> gracefully to "unknown" on any error so the lookup can never break your app.

---

## What hexdb.io is

A free, community-maintained database that maps an aircraft's **ICAO 24-bit hex
address → registration, operator/owner, and type**. It's the same class of
backing data (Mictronics / OpenSky-style) that tar1090 and readsb use.

It is a **static registration database, not a live-position feed.** It tells you
*what an airframe is*, not *where it is right now*. Pair it with a live source
(your receiver, adsb.lol, airplanes.live, ADS-B Exchange, OpenSky) for position.

---

## The core call

```
GET https://hexdb.io/api/v1/aircraft/{HEX}
```

- `{HEX}` = the 6-character ICAO hex, e.g. `A1C183` (case-insensitive).
- **No API key, no auth, no required headers.**
- Response `Content-Type: application/json`, served via Cloudflare.
- `Access-Control-Allow-Origin: *` → **CORS is open**, so you can call it
  directly from browser JavaScript with no proxy.
- Typical latency ~0.5–1 s.

### 200 — found

```json
{
  "ModeS": "A1C183",
  "Registration": "N212FX",
  "Manufacturer": "Bell",
  "ICAOTypeCode": "B429",
  "Type": "429 Global Ranger",
  "RegisteredOwners": "Fairfax County Police Department",
  "OperatorFlagCode": "B429"
}
```

| Field | Meaning |
|-------|---------|
| `ModeS` | the ICAO hex you queried |
| `Registration` | tail number / registration (e.g. `N212FX`) |
| `Manufacturer` | e.g. `Bell` |
| `Type` | model name, e.g. `429 Global Ranger` |
| `ICAOTypeCode` | ICAO type designator, e.g. `B429` |
| `RegisteredOwners` | registered owner/operator |
| `OperatorFlagCode` | operator flag/livery code (often mirrors type) |

All values are strings.

### 404 — not found (covert, deregistered, or simply unknown)

```json
{"status":"404","error":"Aircraft not found."}
```

> **Important:** the 404 body is *also* JSON, so don't assume `.json()` implies
> success. **Branch on the HTTP status code**, not on whether the body parses.

---

## Sibling endpoints (same host, same no-auth / open-CORS rules)

```
GET https://hexdb.io/api/v1/route/icao/{CALLSIGN}
    → {"flight":"BAW294","route":"KORD-EGLL","updatetime":1293980470}

GET https://hexdb.io/api/v1/airport/icao/{ICAO}
    → {"airport":"John F. Kennedy International Airport","iata":"JFK",
       "icao":"KJFK","latitude":40.6397,"longitude":-73.7789,
       "country_code":"US","region_name":"New York"}
```

⚠️ **Route data is crowd-sourced and often stale** (observed `updatetime`
values years old). Treat routes as a hint, not ground truth. Aircraft
registration data is far more reliable than route data.

---

## The robustness pattern (matters more than the endpoint)

Wrap the lookup so it can **never break the main app**:

1. **Cache with a TTL.** Key by hex; keep results ~24 h. Registrations rarely
   change and you'll see the same aircraft repeatedly. **Cache the 404 misses
   too**, or an aircraft that's permanently not in the DB (e.g. a covert plane
   you see constantly) will hammer the API on every sighting.
2. **Short timeout** (~3–4 s). A slow or down API must not stall your pipeline.
3. **Degrade gracefully.** 404, timeout, connection error, or malformed JSON →
   return `None`/empty and carry on. The lookup only *enriches*; it must never
   gate core logic.
4. **Be a good citizen.** It's a free community service. The cache is what keeps
   your request rate low. Send a descriptive `User-Agent`, and don't fan out
   hundreds of uncached parallel requests.

---

## Reference implementations

### Python

```python
import time, requests

_cache = {}          # hex -> (fetched_at, info_or_None)
TTL = 86400          # 24h

def lookup_aircraft(hex_code, timeout=4):
    """hex -> {reg, owner, type} or None. Never raises."""
    key = hex_code.strip().upper()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    info = None
    try:
        r = requests.get(
            f"https://hexdb.io/api/v1/aircraft/{key}",
            timeout=timeout,
            headers={"User-Agent": "my-aircraft-app/1.0"},
        )
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, dict) and d.get("Registration"):
                info = {
                    "reg":   d.get("Registration", ""),
                    "owner": d.get("RegisteredOwners", ""),
                    "type":  d.get("Type", "") or d.get("Manufacturer", ""),
                }
        # 404 / anything else -> info stays None
    except (requests.RequestException, ValueError):
        pass  # network error or bad JSON -> degrade to None
    _cache[key] = (time.time(), info)   # cache misses too
    return info
```

### Browser JavaScript (works thanks to open CORS)

```js
const cache = new Map();

async function lookupAircraft(hex, ttlMs = 864e5 /* 24h */) {
  const key = hex.trim().toUpperCase();
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < ttlMs) return hit.v;

  let info = null;
  try {
    const r = await fetch(`https://hexdb.io/api/v1/aircraft/${key}`, {
      signal: AbortSignal.timeout(4000),
    });
    if (r.ok) {
      const d = await r.json();
      if (d && d.Registration) {
        info = { reg: d.Registration, owner: d.RegisteredOwners || "",
                 type: d.Type || d.Manufacturer || "" };
      }
    }
  } catch {
    /* timeout / network error -> null */
  }
  cache.set(key, { t: Date.now(), v: info });   // cache misses too
  return info;
}
```

---

## Caveats worth knowing

- **Covert / surveillance and freshly (de)registered aircraft are frequently
  absent** (that's the 404). Use registration lookups to *enrich* and *confirm*
  — **never to decide whether something is interesting**, or you'll filter out
  exactly the aircraft that matter. Decide "interesting" from ICAO hex-range
  membership and/or callsign matching; let hexdb only add identity on top.
- **It's one community database.** Cross-check or substitute when you need
  authority:
  - **FAA registry** — ground truth for US-civil aircraft. Every US `A`-block
    hex (`A00000`–`ADF7C7`) maps deterministically to an N-number; look that up
    in the FAA Releasable Aircraft Database (updated daily).
  - **ADS-B Exchange** — the strongest source for military / blocked / covert
    aircraft because it doesn't filter; API is paid (via RapidAPI).
  - **OpenSky Network** — free for research; downloadable aircraft-metadata CSV
    for bulk hex→metadata joins (its old live-metadata REST endpoint is retired).
  - **adsb.lol / airplanes.live / adsb.fi** — free, unfiltered *live* feeds by
    hex (position/callsign if currently transmitting), good complements to the
    static registration data here.
- hexdb is the best **free, no-key, CORS-friendly** option for quick
  hex→identity, which is why it's a sensible default for enrichment.

---

*Verified live against hexdb.io on 2026-10-02: the `aircraft`, `route`, and
`airport` endpoints respond as documented; aircraft lookups need no API key and
return `Access-Control-Allow-Origin: *`.*

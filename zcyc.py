"""MOEX КБД (G-curve) — zero-coupon yield curve via ISS yearyields."""
import time
from datetime import date, timedelta
import aiohttp

_CACHE: tuple[float, list] | None = None
_CACHE_PREV: dict[str, list] = {}  # date_str -> points
_TTL = 3600  # 1 hour


async def _fetch_zcyc_for_date(date_str: str | None) -> list[tuple[float, float]]:
    """Fetch КБД points for a given date (YYYY-MM-DD) or current if None."""
    url = "https://iss.moex.com/iss/engines/stock/zcyc.json"
    params: dict = {"iss.meta": "off", "iss.only": "yearyields"}
    if date_str:
        params["date"] = date_str
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "bondify/1.0"}
        ) as s:
            async with s.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as r:
                data = await r.json(content_type=None)
    except Exception:
        return []

    cols = data.get("yearyields", {}).get("columns", [])
    rows = data.get("yearyields", {}).get("data", [])
    points: list[tuple[float, float]] = []
    for row in rows:
        d = dict(zip(cols, row))
        t, y = d.get("period"), d.get("value")
        if t is not None and y is not None:
            points.append((float(t), float(y)))
    points.sort(key=lambda p: p[0])
    return points


async def fetch_zcyc() -> list[tuple[float, float]]:
    """Return current КБД points: sorted list of (period_years, yield_pct)."""
    global _CACHE
    now = time.time()
    if _CACHE and now - _CACHE[0] < _TTL:
        return _CACHE[1]

    points = await _fetch_zcyc_for_date(None)
    if not points and _CACHE:
        return _CACHE[1]
    _CACHE = (now, points)
    return points


async def fetch_zcyc_prev() -> tuple[list[tuple[float, float]], str]:
    """Return (points, date_str) for previous trading session КБД.

    Walks back up to 7 calendar days skipping weekends and holidays
    (days where MOEX returns empty data).
    """
    today = date.today()
    for delta in range(1, 8):
        d = today - timedelta(days=delta)
        if d.weekday() >= 5:  # skip Sat/Sun
            continue
        ds = d.isoformat()
        if ds in _CACHE_PREV:
            return _CACHE_PREV[ds], ds
        points = await _fetch_zcyc_for_date(ds)
        if points:
            _CACHE_PREV[ds] = points
            return points, ds
    return [], ""


def zcyc_yield(t: float, points: list[tuple[float, float]]) -> float | None:
    """Interpolate KBD yield (%) at duration t years."""
    if not points:
        return None
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        if x0 <= t <= x1:
            return y0 + (y1 - y0) * (t - x0) / (x1 - x0)
    return None

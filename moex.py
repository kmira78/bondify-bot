"""MOEX ISS API — async bond fetching with 5-minute cache."""
import asyncio
import time
import aiohttp
from datetime import date, datetime

# Board groups
BOARDS = {
    "ofz":   ["TQOB"],
    "corp":  ["TQCB", "TQRD"],
    "usd":   ["TQOD"],
    "cny":   ["TQOY"],
    "all":   ["TQOB", "TQCB", "TQRD"],
}

_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 300  # 5 minutes
PAGE_SIZE = 500


async def _fetch_page(session: aiohttp.ClientSession, board_id: str, start: int) -> dict:
    url = (
        f"https://iss.moex.com/iss/engines/stock/markets/bonds"
        f"/boards/{board_id}/securities.json"
    )
    params = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": "SECID,SHORTNAME,FACEVALUE,COUPONPERCENT,COUPONVALUE,MATDATE,LISTLEVEL,BONDTYPE,NEXTCOUPON,COUPONPERIOD",
        "marketdata.columns": "SECID,YIELD,LAST",
        "start": start,
        "limit": PAGE_SIZE,
    }
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20), ssl=False) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


def _parse_pages(pages_data: list[dict], board_id: str) -> list[dict]:
    today = date.today()
    result = []
    for data in pages_data:
        sec = data.get("securities", {})
        mkt = data.get("marketdata", {})
        sc = sec.get("columns", [])
        sd = sec.get("data", [])
        mc = mkt.get("columns", [])
        md = mkt.get("data", [])

        mkt_map = {dict(zip(mc, r))["SECID"]: dict(zip(mc, r)) for r in md}

        for row in sd:
            d = dict(zip(sc, row))
            matdate_str = d.get("MATDATE") or ""
            if not matdate_str or matdate_str == "0000-00-00":
                continue
            try:
                matdate = datetime.strptime(matdate_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            days = (matdate - today).days
            if days <= 0:
                continue
            m = mkt_map.get(d["SECID"], {})
            name = (d["SHORTNAME"] or d["SECID"]).upper()
            bondtype = (d.get("BONDTYPE") or "").lower()
            if "флоатер" in bondtype or "floater" in bondtype:
                if any(k in name for k in ("КС", "KEY", "КЛЮЧ")):
                    coupon_type = "keyrate"
                elif "RUONIA" in name or "РУОНИА" in name:
                    coupon_type = "ruonia"
                else:
                    coupon_type = "floater"
            elif "фикс" in bondtype or "fix" in bondtype:
                coupon_type = "fixed"
            elif "дисконт" in bondtype:
                coupon_type = "zero"
            elif "валют" in bondtype:
                coupon_type = "fx"
            elif not d.get("COUPONPERCENT"):
                coupon_type = "zero"
            else:
                coupon_type = "fixed"

            cp = d.get("COUPONPERIOD")
            result.append({
                "secid": d["SECID"],
                "name": d["SHORTNAME"] or d["SECID"],
                "face": d.get("FACEVALUE") or 1000,
                "coupon": d.get("COUPONPERCENT"),
                "coupon_value": d.get("COUPONVALUE"),
                "coupon_type": coupon_type,
                "matdate": matdate_str,
                "days": days,
                "listlevel": d.get("LISTLEVEL") or 3,
                "yield": m.get("YIELD"),
                "price": m.get("LAST"),
                "board": board_id,
                "next_coupon": d.get("NEXTCOUPON"),
                "coupon_period": int(cp) if cp else 182,
            })
    return result


async def _fetch_board_async(board_id: str) -> list[dict]:
    now = time.time()
    if board_id in _CACHE:
        ts, data = _CACHE[board_id]
        if now - ts < _CACHE_TTL:
            return data

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(
        headers={"User-Agent": "bondify-clone/1.0"},
        connector=connector,
    ) as session:
        first = await _fetch_page(session, board_id, 0)
        first_rows = first.get("securities", {}).get("data", [])
        if not first_rows:
            _CACHE[board_id] = (now, [])
            return []

        starts = list(range(PAGE_SIZE, 15_000, PAGE_SIZE))
        tasks = [_fetch_page(session, board_id, s) for s in starts]
        rest = await asyncio.gather(*tasks, return_exceptions=True)

    pages = [first]
    for r in rest:
        if isinstance(r, Exception):
            break
        rows = r.get("securities", {}).get("data", [])
        if not rows:
            break
        pages.append(r)

    bonds = _parse_pages(pages, board_id)
    _CACHE[board_id] = (now, bonds)
    return bonds


async def fetch_bonds(board_type: str) -> list[dict]:
    """Fetch all bonds for a board group (deduplicated)."""
    boards = BOARDS.get(board_type, [board_type])
    board_results = await asyncio.gather(*[_fetch_board_async(b) for b in boards])
    seen: set[str] = set()
    result = []
    for bonds in board_results:
        for b in bonds:
            if b["secid"] not in seen:
                seen.add(b["secid"])
                result.append(b)
    return result


async def fetch_bond_by_secid(secid: str) -> dict | None:
    """Find a single bond across all boards."""
    all_boards = ["TQOB", "TQCB", "TQRD", "TQOD", "TQOY"]
    board_results = await asyncio.gather(*[_fetch_board_async(b) for b in all_boards])
    for bonds in board_results:
        for b in bonds:
            if b["secid"].upper() == secid.upper():
                return b
    return None


def compute_ofz_duration(bond: dict) -> float | None:
    """Macaulay duration (years) via Cbonds Actual/365F. Returns None if data missing."""
    try:
        coupon_rate = float(bond.get("coupon") or 0)
        if coupon_rate == 0:
            return None
        coupon_period = int(bond.get("coupon_period") or 182)
        if coupon_period <= 0:
            return None
        next_coupon_str = (bond.get("next_coupon") or "")[:10]
        matdate_str = (bond.get("matdate") or "")[:10]
        price = bond.get("price")
        if not price or not next_coupon_str or not matdate_str:
            return None

        next_coupon = datetime.strptime(next_coupon_str, "%Y-%m-%d").date()
        matdate = datetime.strptime(matdate_str, "%Y-%m-%d").date()
        today = date.today()

        if next_coupon <= today:
            return None

        n_coupons = max(1, round((matdate - next_coupon).days / coupon_period) + 1)

        from bond_calc import BondSpec, calc_from_price
        spec = BondSpec(
            coupon_rate=coupon_rate,
            coupon_period_days=coupon_period,
            n_coupons=n_coupons,
            settle_date=today,
            first_coupon_date=next_coupon,
        )
        r = calc_from_price(spec, clean_price=float(price))
        return r.duration_years
    except Exception:
        return None


async def warmup_cache():
    """Pre-fetch all main boards (errors silenced)."""
    import logging
    all_boards = ["TQOB", "TQCB", "TQRD", "TQOD", "TQOY"]
    try:
        await asyncio.gather(*[_fetch_board_async(b) for b in all_boards])
        logging.getLogger(__name__).info("MOEX cache warmed up OK")
    except Exception as e:
        logging.getLogger(__name__).warning("MOEX warmup skipped: %s", e)

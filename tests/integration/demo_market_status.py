"""Read-only: what does the DEMO server say about some markets right now? DEMO host only; places no orders.

    python tests/integration/demo_market_status.py                 # BTCUSD VIX GOLD US100
    python tests/integration/demo_market_status.py BTCUSD ETHUSD   # the ones you name
    python tests/integration/demo_market_status.py --watch VIX     # poll every 10 s and print each change of status until it is TRADEABLE
                                                                   # (start it a few minutes before a market is due to open: it measures how long
                                                                   #  Capital.com's status lags the published opening time)

Same opt-in variables as the suite (CAPITAL_DEMO_IDENTITY / _PASSWORD / _API_KEY, CAPITAL_DEMO_ALLOW=1). For each market it prints the status Capital.com
reports, the market modes, when the price last updated, the published opening hours for today (UTC) and whether the library's hours helpers say it is open.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))          # for demo_harness

import demo_harness as h
from capital_hook import DEMO_HOST, APIError, CapitalClient, TradeMode, is_market_closed


async def watch(client, epic: str, interval: float = 10.0, until: str = "TRADEABLE", sleep=asyncio.sleep, now=None, out=print, max_polls: int | None = None):
    """Print the market's status whenever it changes; stop once it is ``until``. Returns the last status seen."""
    now = now or (lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    started, last, polls = now(), None, 0
    while True:
        status = (await client.get_market(epic))["snapshot"].get("marketStatus")
        polls += 1
        if status != last:
            out(f"{now():%a %H:%M:%S} UTC  {epic}: {status}")
            last = status
        if status == until:
            out(f"{epic} is {status} {int((now() - started).total_seconds())} s after the watch began")
            return status
        if max_polls is not None and polls >= max_polls:
            return status
        await sleep(interval)


async def main(epics: list[str], watching: bool = False) -> int:
    import os
    reason = h.missing_requirements()
    if reason or h.dry_run():
        print(reason or "dry-run is not meant for this script")
        return 2
    client = CapitalClient(os.environ["CAPITAL_DEMO_IDENTITY"], os.environ["CAPITAL_DEMO_PASSWORD"], os.environ["CAPITAL_DEMO_API_KEY"], mode=TradeMode.DEMO)
    assert client.host == DEMO_HOST and "demo" in client.host, "refusing to run: not the demo host"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    print(f"now: {now:%a %Y-%m-%d %H:%M:%S} UTC   host: {client.host}\n")
    try:
        await client.login()
        if watching:
            for epic in epics:
                await watch(client, epic)
            return 0
        for epic in epics:
            try:
                market = await client.get_market(epic)
            except APIError as exc:
                print(f"{epic}: {exc.status_code} {exc.error_code}\n")
                continue
            snapshot, instrument = market["snapshot"], market["instrument"]
            hours = instrument.get("openingHours") or {}
            today = hours.get(now.strftime("%a").lower())
            print(f"{epic}  ({instrument.get('name')}, {instrument.get('type')})")
            print(f"  marketStatus : {snapshot.get('marketStatus')}      marketModes: {snapshot.get('marketModes')}")
            print(f"  bid / offer  : {snapshot.get('bid')} / {snapshot.get('offer')}      price updated: {snapshot.get('updateTime')}")
            print(f"  hours today  : {today}   (zone {hours.get('zone')})   library says {'CLOSED' if is_market_closed(hours, minutes=0, now=now) else 'open'}\n")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    arguments = [a for a in sys.argv[1:] if a != "--watch"]
    sys.exit(asyncio.run(main(arguments or ["BTCUSD", "VIX", "GOLD", "US100"], watching="--watch" in sys.argv[1:])))

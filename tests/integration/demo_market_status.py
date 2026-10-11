"""Read-only: what does the DEMO server say about some markets right now? DEMO host only; places no orders.

    python tests/integration/demo_market_status.py                 # BTCUSD VIX GOLD US100
    python tests/integration/demo_market_status.py BTCUSD ETHUSD   # the ones you name

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


async def main(epics: list[str]) -> int:
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
    sys.exit(asyncio.run(main(sys.argv[1:] or ["BTCUSD", "VIX", "GOLD", "US100"])))

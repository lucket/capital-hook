"""Look at (and optionally clean) what the DEMO suite trades on. DEMO host only.

    python tests/integration/demo_cleanup.py            # lists open positions and working orders on the test epic
    python tests/integration/demo_cleanup.py --yes      # ...and closes / cancels them

Same opt-in variables as the suite (CAPITAL_DEMO_IDENTITY / _PASSWORD / _API_KEY / CAPITAL_DEMO_ALLOW=1);
the epic is CAPITAL_DEMO_EPIC or BTCUSD. Only that epic is touched.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))          # for demo_harness

import demo_harness as h
from capital_hook import DEMO_HOST, CapitalClient, TradeMode


async def main(apply: bool) -> int:
    reason = h.missing_requirements()
    if reason or h.dry_run():
        print(reason or "dry-run is not meant for this script")
        return 2
    epic = os.environ.get("CAPITAL_DEMO_EPIC") or "BTCUSD"
    client = CapitalClient(os.environ["CAPITAL_DEMO_IDENTITY"], os.environ["CAPITAL_DEMO_PASSWORD"], os.environ["CAPITAL_DEMO_API_KEY"], mode=TradeMode.DEMO)
    assert client.host == DEMO_HOST and "demo" in client.host, "refusing to run: not the demo host"
    try:
        await client.login()
        positions = [p for p in await client.get_positions() if p["epic"] == epic]
        orders = [o for o in await client.get_working_orders() if o["epic"] == epic]
        print(f"{epic} on the DEMO account: {len(positions)} position(s), {len(orders)} working order(s)")
        for p in positions:
            print(f"  position {p['deal_id']}: {p['direction']} {p['size']} @ {p['level']}")
        for o in orders:
            print(f"  order    {o['deal_id']}: {o['direction']} {o['size']} @ {o['level']}")
        if apply:
            for p in positions:
                await client.close_position(p["deal_id"])
            for o in orders:
                await client.cancel_working_order(o["deal_id"])
            print("closed / cancelled.")
        elif positions or orders:
            print("nothing changed; run again with --yes to close / cancel them.")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--yes" in sys.argv)))

"""Ticker mapping between providers.

Resolves a source-provider ticker (e.g. TradingView ``CBOE:AVIX``) to the
epic used by the executing provider (e.g. Capital.com ``VIX``).

Configuration lives in four tables — ``provider``, ``markets``, ``ticker`` and
``ticker_mapping`` — and can be imported from / exported to a single JSON blob
so the whole mapping can be dropped into the ``TICKER_CONFIG`` environment
variable. See :func:`import_config` / :func:`export_config`.
"""
import json

import aiosqlite

from logger import Logger

# Seeded on startup when the provider table is empty.
DEFAULT_PROVIDERS = [
    {"id": "TV", "name": "TradingView"},
    {"id": "C", "name": "Capital.com"},
    {"id": "IB", "name": "Interactive Broker"},
]


async def seed_default_providers() -> None:
    """Insert the built-in providers if none exist yet (non-destructive)."""
    from memory import settings

    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.cursor() as cursor:
            await cursor.execute("SELECT COUNT(*) FROM provider")
            (count,) = await cursor.fetchone()
            if count == 0:
                await cursor.executemany(
                    "INSERT OR IGNORE INTO provider (id, name) VALUES (?, ?)",
                    [(p["id"], p["name"]) for p in DEFAULT_PROVIDERS],
                )
        await db.commit()


async def import_config(config: dict) -> None:
    """Upsert providers, markets, tickers and mappings from a config dict.

    Existing rows with the same primary key are replaced; rows not present in
    the config are left untouched (additive/idempotent, never a full wipe).
    """
    from memory import settings

    providers = config.get("providers", []) or []
    markets = config.get("markets", []) or []
    tickers = config.get("tickers", []) or []
    mappings = config.get("mappings", []) or []

    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.cursor() as cursor:
            await cursor.executemany(
                "INSERT OR REPLACE INTO provider (id, name) VALUES (?, ?)",
                [(p["id"], p.get("name", p["id"])) for p in providers],
            )
            await cursor.executemany(
                "INSERT OR REPLACE INTO markets (provider_id, market_id, description) VALUES (?, ?, ?)",
                [(m["provider_id"], m["market_id"], m.get("description")) for m in markets],
            )
            await cursor.executemany(
                "INSERT OR REPLACE INTO ticker (provider_id, ticker, description, market_id) VALUES (?, ?, ?, ?)",
                [(t["provider_id"], t["ticker"], t.get("description"), t.get("market_id")) for t in tickers],
            )
            await cursor.executemany(
                """INSERT OR REPLACE INTO ticker_mapping
                   (source_provider_id, source_ticker, target_provider_id, target_ticker)
                   VALUES (?, ?, ?, ?)""",
                [(x["source_provider_id"], x["source_ticker"], x["target_provider_id"], x["target_ticker"]) for x in mappings],
            )
        await db.commit()

    await Logger.app_log(
        title="TICKER_CONFIG_IMPORT",
        message=f"providers={len(providers)} markets={len(markets)} tickers={len(tickers)} mappings={len(mappings)}",
    )


async def load_config_from_env() -> None:
    """Seed defaults, then import ``TICKER_CONFIG`` JSON from the env if present."""
    from memory import settings

    await seed_default_providers()

    raw = settings.TICKER_CONFIG
    if not raw:
        return
    try:
        config = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        await Logger.app_log(title="TICKER_CONFIG_ERR", message=f"Invalid TICKER_CONFIG JSON: {e}")
        return
    await import_config(config)


async def export_config() -> dict:
    """Dump the full mapping config as a dict (round-trips with import_config)."""
    from memory import settings

    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.cursor() as cursor:
            await cursor.execute("SELECT id, name FROM provider ORDER BY id")
            providers = [{"id": r[0], "name": r[1]} for r in await cursor.fetchall()]

            await cursor.execute("SELECT provider_id, market_id, description FROM markets ORDER BY provider_id, market_id")
            markets = [{"provider_id": r[0], "market_id": r[1], "description": r[2]} for r in await cursor.fetchall()]

            await cursor.execute("SELECT provider_id, ticker, description, market_id FROM ticker ORDER BY provider_id, ticker")
            tickers = [{"provider_id": r[0], "ticker": r[1], "description": r[2], "market_id": r[3]} for r in await cursor.fetchall()]

            await cursor.execute(
                "SELECT source_provider_id, source_ticker, target_provider_id, target_ticker FROM ticker_mapping "
                "ORDER BY source_provider_id, source_ticker, target_provider_id"
            )
            mappings = [
                {"source_provider_id": r[0], "source_ticker": r[1], "target_provider_id": r[2], "target_ticker": r[3]}
                for r in await cursor.fetchall()
            ]

    return {"providers": providers, "markets": markets, "tickers": tickers, "mappings": mappings}


async def resolve_epic(source_provider_id: str, source_ticker: str, target_provider_id: str) -> str | None:
    """Return the executing-provider ticker (epic) for a source ticker, or None.

    Matching on the source ticker is case-insensitive.
    """
    from memory import settings

    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.cursor() as cursor:
            await cursor.execute(
                """SELECT target_ticker FROM ticker_mapping
                   WHERE source_provider_id = ? AND target_provider_id = ?
                   AND source_ticker = ? COLLATE NOCASE
                   LIMIT 1""",
                (source_provider_id, target_provider_id, source_ticker),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

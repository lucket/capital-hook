"""Ticker mapping between providers.

Resolves a source-provider ticker (e.g. TradingView ``CBOE:AVIX``) to the
epic used by the executing provider (e.g. Capital.com ``VIX``).

Configuration lives in four tables — ``provider``, ``markets``, ``ticker`` and
``ticker_mapping`` — and can be imported from / exported to a single JSON blob
so the whole mapping can be dropped into the ``TICKER_CONFIG`` environment
variable. See :func:`import_config` / :func:`export_config`.
"""
import json
import os

import aiosqlite
from dotenv import load_dotenv

from logger import Logger

# Seeded on startup when the provider table is empty.
DEFAULT_PROVIDERS = [
    {"id": "TV", "name": "TradingView"},
    {"id": "C", "name": "Capital.com"},
    {"id": "IB", "name": "Interactive Broker"},
]

# ---------------------------------------------------------------------------
# In-memory "environment" layer.
#
# Parsed from the TICKER_CONFIG env var at startup (and on demand via reload).
# It is kept entirely SEPARATE from the SQLite tables: env mappings ALWAYS win
# over the DB at resolution time, and env rows are shown locked/read-only in the
# UI. Because they never touch the DB, they can't be edited or deleted from the
# Mapping page — deleting/adding a DB row with the same key simply has no effect
# on the env layer.
# ---------------------------------------------------------------------------
_env_config: dict = {"providers": [], "markets": [], "tickers": [], "mappings": []}
_env_index: dict = {}  # (source_provider, target_provider, source_ticker_lower) -> target_ticker


def _rebuild_env_index() -> None:
    global _env_index
    idx = {}
    for m in _env_config.get("mappings", []):
        try:
            key = (m["source_provider_id"], m["target_provider_id"], str(m["source_ticker"]).lower())
            idx[key] = m["target_ticker"]
        except (KeyError, TypeError):
            continue
    _env_index = idx


async def load_env_mappings(reload_dotenv: bool = False) -> int:
    """(Re)build the in-memory env layer from TICKER_CONFIG. Returns mapping count.

    With ``reload_dotenv`` the ``.env`` file is re-read first, so the layer can
    be refreshed without restarting the process.
    """
    from memory import settings
    global _env_config

    if reload_dotenv:
        load_dotenv(override=True)
        settings.TICKER_CONFIG = os.getenv("TICKER_CONFIG")
        settings.DEFAULT_SOURCE_PROVIDER = os.getenv("DEFAULT_SOURCE_PROVIDER", "TV")
        settings.EXECUTING_PROVIDER = os.getenv("EXECUTING_PROVIDER", "C")

    cfg = {"providers": [], "markets": [], "tickers": [], "mappings": []}
    raw = settings.TICKER_CONFIG
    if raw:
        try:
            parsed = json.loads(raw)
            for k in cfg:
                cfg[k] = parsed.get(k, []) or []
        except (json.JSONDecodeError, TypeError) as e:
            await Logger.app_log(title="TICKER_CONFIG_ERR", message=f"Invalid TICKER_CONFIG JSON: {e}")

    _env_config = cfg
    _rebuild_env_index()
    await Logger.app_log(title="TICKER_ENV_LOADED", message=f"env mappings={len(_env_index)}")
    return len(_env_index)


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
    """Startup hook: seed default providers into the DB and build the in-memory
    env layer from ``TICKER_CONFIG`` (the env layer is NOT written to the DB)."""
    await seed_default_providers()
    await load_env_mappings()


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


# ---------------------------------------------------------------------------
# CRUD helpers (used by the mapping management UI)
# ---------------------------------------------------------------------------

async def _run(query: str, params: tuple = (), *, fetch: str = None):
    from memory import settings
    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.cursor() as cursor:
            await cursor.execute(query, params)
            if fetch == "all":
                rows = await cursor.fetchall()
        await db.commit()
    return rows if fetch == "all" else None


async def list_providers() -> list:
    rows = await _run("SELECT id, name FROM provider ORDER BY id", fetch="all")
    db = [{"id": r[0], "name": r[1], "locked": False} for r in rows]
    env = [{"id": p["id"], "name": p.get("name", p["id"]), "locked": True} for p in _env_config.get("providers", [])]
    env_keys = {e["id"] for e in env}
    return env + [d for d in db if d["id"] not in env_keys]


async def add_provider(id: str, name: str) -> None:
    await _run("INSERT OR REPLACE INTO provider (id, name) VALUES (?, ?)", (id, name or id))


async def delete_provider(id: str) -> None:
    await _run("DELETE FROM provider WHERE id = ?", (id,))


async def list_markets() -> list:
    rows = await _run("SELECT provider_id, market_id, description FROM markets ORDER BY provider_id, market_id", fetch="all")
    db = [{"provider_id": r[0], "market_id": r[1], "description": r[2], "locked": False} for r in rows]
    env = [{"provider_id": m["provider_id"], "market_id": m["market_id"], "description": m.get("description"), "locked": True}
           for m in _env_config.get("markets", [])]
    env_keys = {(e["provider_id"], e["market_id"]) for e in env}
    return env + [d for d in db if (d["provider_id"], d["market_id"]) not in env_keys]


async def add_market(provider_id: str, market_id: str, description: str = None) -> None:
    await _run("INSERT OR REPLACE INTO markets (provider_id, market_id, description) VALUES (?, ?, ?)", (provider_id, market_id, description))


async def delete_market(provider_id: str, market_id: str) -> None:
    await _run("DELETE FROM markets WHERE provider_id = ? AND market_id = ?", (provider_id, market_id))


async def list_tickers(provider_id: str = None, q: str = None) -> list:
    query = "SELECT provider_id, ticker, description, market_id FROM ticker"
    clauses, params = [], []
    if provider_id:
        clauses.append("provider_id = ?"); params.append(provider_id)
    if q:
        clauses.append("ticker LIKE ? COLLATE NOCASE"); params.append(f"%{q}%")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY provider_id, ticker LIMIT 50"
    rows = await _run(query, tuple(params), fetch="all")
    db = [{"provider_id": r[0], "ticker": r[1], "description": r[2], "market_id": r[3], "locked": False} for r in rows]

    env = []
    for t in _env_config.get("tickers", []):
        if provider_id and t.get("provider_id") != provider_id:
            continue
        if q and q.lower() not in str(t.get("ticker", "")).lower():
            continue
        env.append({"provider_id": t["provider_id"], "ticker": t["ticker"],
                    "description": t.get("description"), "market_id": t.get("market_id"), "locked": True})
    env_keys = {(e["provider_id"], e["ticker"]) for e in env}
    return env + [d for d in db if (d["provider_id"], d["ticker"]) not in env_keys]


async def add_ticker(provider_id: str, ticker: str, description: str = None, market_id: str = None) -> None:
    await _run(
        "INSERT OR REPLACE INTO ticker (provider_id, ticker, description, market_id) VALUES (?, ?, ?, ?)",
        (provider_id, ticker, description, market_id),
    )


async def delete_ticker(provider_id: str, ticker: str) -> None:
    await _run("DELETE FROM ticker WHERE provider_id = ? AND ticker = ?", (provider_id, ticker))


async def list_mappings() -> list:
    rows = await _run(
        "SELECT source_provider_id, source_ticker, target_provider_id, target_ticker FROM ticker_mapping "
        "ORDER BY source_provider_id, source_ticker, target_provider_id",
        fetch="all",
    )
    db = [{"source_provider_id": r[0], "source_ticker": r[1], "target_provider_id": r[2], "target_ticker": r[3], "locked": False} for r in rows]
    env = [{"source_provider_id": m["source_provider_id"], "source_ticker": m["source_ticker"],
            "target_provider_id": m["target_provider_id"], "target_ticker": m["target_ticker"], "locked": True}
           for m in _env_config.get("mappings", [])]
    env_keys = {(e["source_provider_id"], e["source_ticker"], e["target_provider_id"]) for e in env}
    return env + [d for d in db if (d["source_provider_id"], d["source_ticker"], d["target_provider_id"]) not in env_keys]


async def add_mapping(source_provider_id: str, source_ticker: str, target_provider_id: str, target_ticker: str) -> None:
    await _run(
        """INSERT OR REPLACE INTO ticker_mapping
           (source_provider_id, source_ticker, target_provider_id, target_ticker) VALUES (?, ?, ?, ?)""",
        (source_provider_id, source_ticker, target_provider_id, target_ticker),
    )


async def delete_mapping(source_provider_id: str, source_ticker: str, target_provider_id: str) -> None:
    await _run(
        "DELETE FROM ticker_mapping WHERE source_provider_id = ? AND source_ticker = ? AND target_provider_id = ?",
        (source_provider_id, source_ticker, target_provider_id),
    )


async def resolve_epic(source_provider_id: str, source_ticker: str, target_provider_id: str) -> str | None:
    """Return the executing-provider ticker (epic) for a source ticker, or None.

    Resolution order: the in-memory environment layer wins first (no DB hit);
    the DB is only consulted as a fallback. Matching is case-insensitive.
    """
    # 1) environment layer (always wins, served from memory)
    env_hit = _env_index.get((source_provider_id, target_provider_id, str(source_ticker or "").lower()))
    if env_hit:
        return env_hit

    # 2) DB fallback
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

import asyncio
import logging
import socket
import time

import psycopg

from pg_debug.db import driver as d


def test_diagnose(caplog):
    caplog.set_level(logging.DEBUG)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"postgresql://nobody@127.0.0.1:{port}/db"
    t0 = time.monotonic()

    async def plain():
        try:
            await psycopg.AsyncConnection.connect(url, connect_timeout=20)
        except Exception as e:
            print("PLAIN", round(time.monotonic() - t0, 2), type(e).__name__, repr(str(e))[:300])

    asyncio.run(plain(), loop_factory=asyncio.SelectorEventLoop)
    try:
        psycopg.Connection.connect(url, connect_timeout=20)
    except Exception as e:
        print("SYNC", round(time.monotonic() - t0, 2), type(e).__name__, repr(str(e))[:300])

    pool = d.DbConnPool(url)
    original = pool._connect_failed

    def spy(built, error):
        p = built[0] if built else None
        print("FAILED", round(time.monotonic() - t0, 2), repr(str(error))[:200], getattr(error, "sqlstate", None),
              p.get_stats() if p else None, "borrowed", pool._borrowed, "same", p is pool.pool, "has", pool._has_connected)
        original(built, error)

    pool._connect_failed = spy  # type: ignore[method-assign]
    t0 = time.monotonic()
    try:
        asyncio.run(asyncio.wait_for(pool.pool_connect(), timeout=40), loop_factory=asyncio.SelectorEventLoop)
    except BaseException as e:
        print("POOL", round(time.monotonic() - t0, 2), type(e).__name__, repr(str(e))[:300])
    for r in caplog.records:
        if r.name.startswith("psycopg"):
            print("LOG", round(r.created, 2), r.name, r.getMessage()[:200])
    assert False, "diagnostic"

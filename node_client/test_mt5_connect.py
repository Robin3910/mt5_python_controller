"""One-off MT5 connection smoke test (prompts for MT5 credentials at startup)."""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from config import get_settings
from mt5_client import MT5Client, MT5Error
from mt5_prompt import prompt_mt5_credentials

settings = get_settings()
creds = prompt_mt5_credentials()
print("=== MT5 Connection Test ===")
print(f"MT5_MOCK: {settings.mt5_mock}")
print(f"Login:  {creds['mt5_login']}")
print(f"Server: {creds['mt5_server']}")
print(f"Path:   {creds['mt5_path'] or '(mock / not required)'}")
print()

if settings.mt5_mock:
    print("WARNING: MT5_MOCK=true, testing mock instead of real MT5")
    from mock_mt5 import MockMT5Client

    client = MockMT5Client(
        creds["mt5_login"],
        creds["mt5_password"],
        creds["mt5_server"],
        creds["mt5_path"],
    )
else:
    try:
        import MetaTrader5 as mt5

        print(f"MetaTrader5 package version: {getattr(mt5, '__version__', 'installed')}")
    except ImportError as e:
        print(f"FAIL: MetaTrader5 not installed: {e}")
        sys.exit(1)
    client = MT5Client(
        creds["mt5_login"],
        creds["mt5_password"],
        creds["mt5_server"],
        creds["mt5_path"],
    )

try:
    ok = client.connect()
    if not ok:
        print("FAIL: connect() returned False")
        sys.exit(1)
    print("OK: connect() succeeded")

    info = client.account_info()
    if not info:
        print("FAIL: account_info() returned empty")
        client.disconnect()
        sys.exit(1)

    print("--- Account Info ---")
    for k, v in info.items():
        print(f"  {k}: {v}")

    positions = client.positions()
    print(f"--- Positions: {len(positions)} open ---")
    for p in positions[:5]:
        print(
            f"  ticket={p['ticket']} {p['symbol']} {p['type']} "
            f"vol={p['volume']} profit={p['profit']}"
        )

    prices = client.prices(settings.watchlist[:3])
    print("--- Sample Prices ---")
    for sym, price in prices.items():
        print(f"  {sym}: {price}")
    if not prices:
        print("  (no prices fetched - symbols may not be available)")

    client.disconnect()
    print()
    print("RESULT: MT5 connection test PASSED")
except MT5Error as e:
    print(f"FAIL: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
    sys.exit(1)

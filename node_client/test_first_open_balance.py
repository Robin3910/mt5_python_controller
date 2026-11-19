from types import SimpleNamespace

import pytest

import mt5_client


@pytest.mark.parametrize("retry_balance", [0.0, None])
def test_balance_is_captured_before_successful_order_attempt(monkeypatch, retry_balance):
    calls = []
    balances = iter([1000.0, retry_balance])
    replies = iter([
        SimpleNamespace(retcode=mt5_client.RET_INVALID_FILL),
        SimpleNamespace(retcode=mt5_client.RET_DONE_PARTIAL, order=31, deal=32, volume=0.1, price=10.0),
    ])

    def account_info():
        calls.append("balance")
        balance = next(balances)
        if balance is None:
            raise RuntimeError("account not available")
        return SimpleNamespace(balance=balance)

    def order_send(_request):
        calls.append("send")
        return next(replies)

    monkeypatch.setattr(mt5_client, "mt5", SimpleNamespace(
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1, ORDER_TIME_GTC=0,
        symbol_info_tick=lambda _: SimpleNamespace(ask=10.0, bid=9.9),
        account_info=account_info, order_send=order_send,
    ))
    client = mt5_client.MT5Client(1, "", "Demo")
    monkeypatch.setattr(client, "ensure", lambda: None)
    monkeypatch.setattr(client, "resolve_symbol", lambda symbol: symbol)
    monkeypatch.setattr(client, "_link_block", lambda symbol: None)
    monkeypatch.setattr(client, "filling_modes", lambda symbol: [0, 1])

    report = client.place_market_order("XAUUSD", "BUY", 0.2, capture_balance_before_open=True)
    assert calls == ["balance", "send", "balance", "send"]
    assert report["success"] is True and report["deal"] == 32
    assert report.get("balance_before_open") == retry_balance

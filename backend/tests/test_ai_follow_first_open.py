import pytest

from app import deal_store, group_persist, persist, ws_gateway
from app.connections import manager
from app.state import state
from tests.test_ai_channel_review import apply, approve, setup
from tests.test_ai_follow_api import _mk_ai_strategy, _mk_group, client
from tests.test_ai_follow_settings import open_round


async def report(node_id, dispatch, deal, balance=None, *, success=True, pending=False):
    await ws_gateway._on_trade_result(node_id, {
        "magic": dispatch["magic"], "symbol": "XAUUSD", "success": success, "pending": pending,
        "order": deal, "deal": deal, "retcode": 10008 if pending else 10009 if success else 10006,
        "balance_before_open": balance,
    })


def amounts(client, data):
    headers = data["bob_h"]
    group_id = data["ai_group_id"]
    cards = client.get("/api/ai-follow/me", headers=headers).json()["following"]
    card = next(item for item in cards if item["robot"]["group_id"] == group_id)
    detail = client.get(f"/api/ai-follow/me/{data['host']['id']}", headers=headers,
                        params={"group_id": group_id}).json()
    summary = client.get("/api/ai-follow/nodes", headers=headers).json()["summary"]
    return card["investment_amount"], detail["metrics"]["investment_amount"], summary["investment_amount"]


def test_first_open_balance_is_durable_and_summed_per_account(client, monkeypatch):
    data = setup(client)
    accounts = [data["b_follow"], data["b_primary"]]
    commands = []
    events = []

    async def send(node_id, command):
        commands.append(command)
        return True

    async def broadcast(message):
        events.append(message)

    monkeypatch.setattr(manager, "send_to_node", send)
    monkeypatch.setattr(manager, "broadcast_admin", broadcast)

    async def place(node_id, deal, balance, success=True, pending=False):
        run = await open_round({**data, "b_follow": node_id}, f"first-{deal}")
        assert run["status"] == "sent"
        await report(node_id, run, deal, balance, success=success, pending=pending)
        return run

    approve(client, data, apply(client, data, accounts)["pending"][0]["request_id"])
    client.portal.call(state.store.save_account, accounts[0], {"balance": 9000, "positions": []})
    client.portal.call(place, accounts[0], 100, 9000, False)
    client.portal.call(place, accounts[0], 101, 9000, True, True)
    assert amounts(client, data) == (None, None, None)

    run = client.portal.call(place, accounts[0], 102, 1200.25)
    client.portal.call(report, accounts[0], run, 102, 9000)
    assert amounts(client, data) == (None, None, None)
    client.portal.call(place, accounts[1], 200, 0.0)
    assert amounts(client, data) == (1200.25, 1200.25, 1200.25)
    assert all(command["entry"]["capture_balance_before_open"] is True for command in commands)
    assert all("balance_before_open" not in event.get("data", {}) for event in events)

    client.portal.call(persist.purge_trade_logs)
    assert client.post("/api/ai-follow/leave", headers=data["bob_h"], json={
        "leader_user_id": data["host"]["id"], "group_ids": [data["ai_group_id"]], "node_ids": accounts,
    }).status_code == 200
    approve(client, data, apply(client, data, accounts)["pending"][0]["request_id"])
    client.portal.call(place, accounts[0], 105, 9999)
    assert amounts(client, data) == (1200.25, 1200.25, 1200.25)
    owner = client.get("/api/ai-follow/me", headers=data["host_h"]).json()["following"][0]
    assert owner["investment_amount"] is None


@pytest.mark.parametrize("historical_report", ["trade_result", "deals_report", "delayed_deals_report"])
def test_legacy_first_open_without_balance_cannot_be_replaced_by_later_trade(client, monkeypatch, historical_report):
    data = setup(client)
    approve(client, data, apply(client, data)["pending"][0]["request_id"])

    async def send(*args):
        return True

    monkeypatch.setattr(manager, "send_to_node", send)

    async def make_trades():
        node_id = data["b_follow"]
        old = await open_round(data, "pre-upgrade")
        old_deal = {"deal_id": 101, "entry_type": "IN", "volume": 0.1,
                    "magic": old["magic"], "symbol": "XAUUSD", "time": 100}
        if historical_report == "trade_result":
            await group_persist.update_dispatch_result(node_id=node_id, dispatch_id=old["dispatch_id"],
                result={"success": True, "order": 100, "deal": 101, "retcode": 10009})
        elif historical_report == "deals_report":
            await deal_store.save_deals(node_id, [old_deal])
        await report(node_id, await open_round(data, "post-upgrade"), 102, 1200)
        if historical_report == "delayed_deals_report":
            await ws_gateway._on_deals_report(node_id, {"deals": [old_deal]})

    client.portal.call(make_trades)
    assert amounts(client, data) == (None, None, None)


def test_summary_counts_shared_account_once_even_when_reports_are_out_of_order(client, monkeypatch):
    data = setup(client)
    strategy = _mk_ai_strategy(client, data["host_h"], "second-robot")
    second_group = _mk_group(client, data["host_h"], [data["h_primary"]], "second-robot", strategy_id=strategy,
                             accept_ai_follow=True)
    second = {**data, "ai_group_id": second_group}
    for group in (data, second):
        approve(client, group, apply(client, group)["pending"][0]["request_id"])

    async def send(*args):
        return True

    monkeypatch.setattr(manager, "send_to_node", send)

    async def make_trades():
        first = await open_round(data, "first-fill")
        await report(data["b_follow"], await open_round(data, "later-fill"), 103, 2000)
        await report(data["b_follow"], await open_round(second, "other-group-fill"), 102, 1500)
        await report(data["b_follow"], first, 101, 1000)

    client.portal.call(make_trades)
    assert amounts(client, data) == (1000, 1000, 1000)
    assert amounts(client, second) == (1500, 1500, 1000)

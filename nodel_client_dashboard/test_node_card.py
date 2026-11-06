"""节点卡：差分更新不重建控件、状态文案切换、空态不崩。"""
from __future__ import annotations

import tkinter as tk

import pytest

import theme
from models import InstanceConfig
from process_manager import ProcessManager
from viewmodels import build_list


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前环境无法创建 Tk：{exc}")
    r.withdraw()
    yield r
    r.destroy()


def _vm(root, **kw):
    mgr = ProcessManager()
    cfg = InstanceConfig.create(name="n1", exe_path="/x/n1.exe", cwd="/x")
    mp = mgr.add(cfg)
    for k, v in kw.items():
        setattr(mp.runtime, k, v)
    return build_list(mgr, None)[0]


def test_card_update_does_not_rebuild(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", ws_state="authenticated")
    card = NodeCard(root, vm)
    title = card._name_label
    cell = card._cells[0]
    card.update(vm)
    assert card._name_label is title
    assert card._cells[0] is cell


def test_card_badge_switches_with_state(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    assert "运行中" in card._badge.cget("text")
    stopped = _vm(root, process_alive=False, health="dead")
    card.update(stopped)
    assert "已停止" in card._badge.cget("text")


def test_card_info_cells_show_values(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", ws_state="authenticated",
             algo_trading_allowed=True)
    card = NodeCard(root, vm)
    assert card._cells[0].value_label.cget("text") == "已连接"
    assert card._cells[1].value_label.cget("text") == "已鉴权"
    assert card._cells[2].value_label.cget("text") == "已开启"


def test_card_uptime_none_shows_dash(root):
    from node_card import NodeCard
    card = NodeCard(root, _vm(root))
    assert card._uptime_label.cget("text") == "--"


def test_card_server_and_id_labels(root):
    """卡片左位是券商服务器名，右位金色胶囊是交易账号。"""
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", mt5_login=88001234,
             node_id="TID-9", mt5_server="gtcglobal-sa-server5")
    card = NodeCard(root, vm)
    assert "gtcglobal-sa-server5" in card._server_label.cget("text")
    assert "88001234" in card._id_label.cget("text")


def test_card_falls_back_to_placeholder_when_server_unknown(root):
    """读不到券商服务器名时显示占位符，不用账号顶替（账号在右边胶囊里）。"""
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", mt5_login=88001234)
    card = NodeCard(root, vm)
    assert "服务器 -" in card._server_label.cget("text")
    assert "88001234" in card._id_label.cget("text")


def test_selected_card_uses_gold_border(root):
    from node_card import NodeCard
    import theme
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    # 在线卡片按 CSS 用微绿描边（.node-card.online {border-color:#2fd38138}）
    assert card.card.cget("border_color") == theme.BORDER_ONLINE
    card.update(_replace_selected(vm, True))
    assert card.card.cget("border_color") == theme.GOLD


def test_offline_card_uses_line_border(root):
    from node_card import NodeCard
    import theme
    card = NodeCard(root, _vm(root, process_alive=False, health="dead"))
    assert card.card.cget("border_color") == theme.LINE


def test_starting_card_uses_amber_border(root):
    from node_card import NodeCard
    import theme
    vm = _vm(root, process_alive=False, busy="starting", health="starting")
    card = NodeCard(root, vm)
    assert card.card.cget("border_color") == theme.BORDER_STARTING
    card.stop_animation()


def test_algo_trading_unknown_is_not_ok(root):
    from node_card import NodeCard
    card = NodeCard(root, _vm(root, process_alive=True, health="ok"))
    # 未知状态不得显示为「已开启」
    assert card._cells[2].value_label.cget("text") != "已开启"


def test_set_grid_columns_repositions_without_rebuild(root):
    from node_card import NodeCard, set_grid_columns
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    grid = tk.Frame(root)
    grid.grid_columnconfigure(0, weight=1)
    card.card.grid(row=0, column=0, sticky="nsew")
    set_grid_columns(grid, 3)
    assert int(card.card.grid_info()["column"]) == 0
    set_grid_columns(grid, 2)
    assert int(card.card.grid_info()["column"]) == 0


def test_set_grid_columns_switches_to_two_columns(root):
    from node_card import NodeCard, set_grid_columns
    vm = _vm(root, process_alive=True, health="ok")
    grid = tk.Frame(root)
    cards = [NodeCard(grid, vm) for _ in range(3)]
    for i, c in enumerate(cards):
        c.card.grid(row=0, column=i, sticky="nsew")
    set_grid_columns(grid, 2)
    positions = [(c.card.grid_info()["row"], c.card.grid_info()["column"]) for c in cards]
    assert positions == [(0, 0), (0, 1), (1, 0)]
    # 旧的 uniform 必须清掉，否则第 3 列的权重会串到前两列
    # （Tk 里没有 uniform 的列读出来是 None）
    assert grid.grid_columnconfigure(2)["uniform"] in ("", None, "gridcol")
    assert grid.grid_columnconfigure(2)["weight"] == 0


def test_set_grid_columns_one_column_stacks_vertically(root):
    from node_card import NodeCard, set_grid_columns
    vm = _vm(root, process_alive=True, health="ok")
    grid = tk.Frame(root)
    cards = [NodeCard(grid, vm) for _ in range(3)]
    for i, c in enumerate(cards):
        c.card.grid(row=0, column=i, sticky="nsew")
    set_grid_columns(grid, 1)
    positions = [(c.card.grid_info()["row"], c.card.grid_info()["column"]) for c in cards]
    assert positions == [(0, 0), (1, 0), (2, 0)]


def test_starting_state_pulses(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=False, busy="starting", health="starting")
    card = NodeCard(root, vm)
    assert card._pulse_job is not None
    card.stop_animation()
    assert card._pulse_job is None


def test_online_state_has_no_pulse(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok")
    card = NodeCard(root, vm)
    assert card._pulse_job is None


def test_card_context_binding_exists(root):
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok")
    seen = []
    card = NodeCard(root, vm, on_context=lambda iid, ev=None: seen.append(iid))
    # 窗口必须可见且控件已映射：Tk 不会把事件派发给未映射的控件
    card.pack(fill="x")
    root.deiconify()
    root.update()
    # CTk 控件的真实点击目标是内部 canvas
    card.canvas.event_generate("<Button-3>")
    root.update()
    assert seen == [vm.id]


def _replace_selected(vm, selected: bool):
    import dataclasses
    return dataclasses.replace(vm, selected=selected)


# ─────────────────────────────────────────────────── CSS 规格对齐

def test_card_uses_css_radius_and_padding(root):
    """`.node-card` 的圆角是 10px（CSS 实测值，不是 8）。"""
    from node_card import NodeCard
    card = NodeCard(root, _vm(root, process_alive=True, health="ok"))
    assert card.card.cget("corner_radius") == 10
    assert card.card.cget("fg_color") == theme.PANEL


def test_info_cell_uses_css_colors(root):
    """`.info-value.ok` 绿色；`.info-value.no` 是 --dim(灰)，不是红色。"""
    from node_card import NodeCard
    # 已鉴权由 ws_state == "authenticated" 推出
    card = NodeCard(root, _vm(root, process_alive=True, health="ok",
                              ws_state="authenticated"))
    ok_cell = card._cells[1]
    assert ok_cell.value_label.cget("text") == "已鉴权"
    assert ok_cell.value_label.cget("text_color") == theme.GREEN
    assert ok_cell.mark_label.cget("text") == "✓"
    assert ok_cell.mark_frame.cget("fg_color") == theme.GREEN_ICO_BG

    card.update(_vm(root, process_alive=True, health="ok", ws_state="connecting"))
    bad_cell = card._cells[1]
    assert bad_cell.value_label.cget("text") == "未鉴权"
    assert bad_cell.value_label.cget("text_color") == theme.DIM
    assert bad_cell.mark_label.cget("text") == "✕"
    assert bad_cell.mark_frame.cget("fg_color") == theme.RED_ICO_BG


def test_id_caption_is_gold_pill(root):
    """`.mt5-id-inline` 是金色文字 + 7px 圆角 + 淡琥珀底。"""
    from node_card import NodeCard
    vm = _vm(root, process_alive=True, health="ok", mt5_login=88001234)
    card = NodeCard(root, vm)
    assert card._id_label.cget("text") == "ID 88001234"
    assert card._id_label.cget("text_color") == theme.GOLD_LIGHT
    assert card._id_label.cget("corner_radius") == 7
    assert card._id_label.cget("fg_color") == theme.AMBER_BADGE_BG


def test_toggle_button_text_follows_alive_state(root):
    """参考稿文案：运行中显示「暂停」，已停止显示「启动」。"""
    from node_card import NodeCard
    card = NodeCard(root, _vm(root, process_alive=True, health="ok"))
    assert card._toggle_btn.cget("text") == "暂停"
    card.update(_vm(root, process_alive=False, health="dead"))
    assert card._toggle_btn.cget("text") == "启动"


def test_status_dot_is_8px(root):
    """`.status-dot` 是 8px 圆点。"""
    import theme
    from node_card import NodeCard
    card = NodeCard(root, _vm(root, process_alive=True, health="ok"))
    assert card._dot.cget("width") == theme.H_DOT == 8
    assert card._dot.cget("height") == 8

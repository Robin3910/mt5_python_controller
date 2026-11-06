"""回归测试：MetaTrader5 必须在开发环境和冻结产物里都可用。

背景：导入节点留空账号时，要附着本机 MT5 终端读取当前登录号，
这依赖 `MetaTrader5` 包。踩过的坑：

  1. 开发 venv 没装 MetaTrader5 -> `import MetaTrader5` 失败 ->
     界面提示「本机没有 MetaTrader5 接口，请手工填写账号」；
  2. 冻结产物里没打包 MetaTrader5（PyInstaller 不会自动发现 .pyd 扩展），
     而且 spec 把 numpy 排除了（MetaTrader5 声明依赖 numpy）-> 同样失败。

这两个都是「静默降级」：不报错，只是功能不可用。所以用测试钉住。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SPEC = Path(__file__).resolve().parent / "dashboard_web.spec"


def test_metatrader5_importable() -> None:
    """开发环境必须装得上 MetaTrader5。"""
    import MetaTrader5 as mt5

    assert hasattr(mt5, "initialize")
    assert hasattr(mt5, "account_info")
    assert hasattr(mt5, "shutdown")


def test_metatrader5_has_core_extension() -> None:
    """核心是 .pyd 扩展，必须真的在包里（否则冻结后 import 失败）。"""
    import MetaTrader5 as mt5

    pkg_dir = Path(mt5.__file__).resolve().parent
    exts = list(pkg_dir.glob("_core*.pyd")) + list(pkg_dir.glob("_core*.so"))
    assert exts, f"MetaTrader5 缺少核心扩展：{pkg_dir}"


def test_spec_collects_metatrader5() -> None:
    """spec 必须显式 collect_all("MetaTrader5")。"""
    src = SPEC.read_text(encoding="utf-8")
    assert 'collect_all("MetaTrader5")' in src, (
        "dashboard_web.spec 必须 collect_all('MetaTrader5')，"
        "否则 PyInstaller 不会打进 .pyd 扩展"
    )


def test_spec_does_not_exclude_numpy() -> None:
    """spec 不能排除 numpy —— MetaTrader5 声明依赖它。"""
    src = SPEC.read_text(encoding="utf-8")
    tree = ast.parse(src)

    excludes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Analysis"):
            continue
        for kw in node.keywords:
            if kw.arg == "excludes":
                excludes = [
                    e.value for e in kw.value.elts if isinstance(e, ast.Constant)
                ]

    assert "numpy" not in excludes, (
        "dashboard_web.spec 不能排除 numpy：MetaTrader5 依赖它，"
        "排除后冻结产物 import MetaTrader5 会失败"
    )


def test_numpy_importable() -> None:
    """numpy 必须跟着装上。"""
    import numpy

    assert numpy.__version__


if __name__ == "__main__":
    tests = [
        test_metatrader5_importable,
        test_metatrader5_has_core_extension,
        test_spec_collects_metatrader5,
        test_spec_does_not_exclude_numpy,
        test_numpy_importable,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"[ok] {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {test.__name__}: {exc}")
    sys.exit(1 if failed else 0)

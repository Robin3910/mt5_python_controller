"""positions_get / orders_get：None 是读失败，不能当成真空仓。"""
import pytest

import mt5_client as mc


def test_book_rows_passthrough_tuple():
    rows = (object(),)
    assert mc.mt5_book_rows(rows, api_name="positions_get") is rows


def test_book_rows_empty_tuple_is_empty():
    assert list(mc.mt5_book_rows((), api_name="positions_get")) == []


def test_book_rows_none_with_ok_code_is_empty(monkeypatch):
    class Fake:
        @staticmethod
        def last_error():
            return (1, "Success")

    monkeypatch.setattr(mc, "mt5", Fake())
    assert list(mc.mt5_book_rows(None, api_name="positions_get")) == []


def test_book_rows_none_with_zero_code_is_empty(monkeypatch):
    class Fake:
        @staticmethod
        def last_error():
            return (0, "Success")

    monkeypatch.setattr(mc, "mt5", Fake())
    assert list(mc.mt5_book_rows(None, api_name="orders_get")) == []


def test_book_rows_none_with_error_raises(monkeypatch):
    class Fake:
        @staticmethod
        def last_error():
            return (-1, "IPC timeout")

    monkeypatch.setattr(mc, "mt5", Fake())
    with pytest.raises(mc.MT5Error, match="IPC timeout"):
        mc.mt5_book_rows(None, api_name="positions_get")

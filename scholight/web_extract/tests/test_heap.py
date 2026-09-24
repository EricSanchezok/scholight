from unittest.mock import Mock

import pytest

from scholight.web_extract import heap


@pytest.fixture(autouse=True)
def clear_cached_symbol():
    heap._load_trim.cache_clear()
    yield
    heap._load_trim.cache_clear()


def test_native_trim_symbol_is_cached_with_the_correct_c_signature(monkeypatch):
    monkeypatch.setattr(heap.sys, "platform", "linux")
    trim = Mock(return_value=1)
    library = Mock(malloc_trim=trim)
    loader = Mock(return_value=library)
    monkeypatch.setattr(heap.ctypes, "CDLL", loader)
    assert heap.release_unused_heap() and heap.release_unused_heap()
    loader.assert_called_once_with(None)
    assert trim.call_count == 2 and trim.call_args.args == (0,)
    assert trim.argtypes == [heap.ctypes.c_size_t] and trim.restype is heap.ctypes.c_int


def test_non_linux_allocator_does_not_load_native_trim(monkeypatch):
    monkeypatch.setattr(heap.sys, "platform", "darwin")
    loader = Mock()
    monkeypatch.setattr(heap.ctypes, "CDLL", loader)
    assert heap.release_unused_heap() is False
    loader.assert_not_called()


@pytest.mark.parametrize("error", [OSError, AttributeError])
def test_missing_native_trim_is_a_cached_noop(monkeypatch, error):
    monkeypatch.setattr(heap.sys, "platform", "linux")
    loader = Mock(side_effect=error)
    monkeypatch.setattr(heap.ctypes, "CDLL", loader)
    assert heap.release_unused_heap() is False
    assert heap.release_unused_heap() is False
    loader.assert_called_once_with(None)


def test_optional_maintenance_failure_does_not_replace_the_document_result(monkeypatch):
    monkeypatch.setattr(heap, "_load_trim", lambda: Mock(side_effect=RuntimeError))
    assert heap.release_unused_heap() is False

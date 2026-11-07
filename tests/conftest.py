"""Shared test setup: every test gets its own empty DA Timer data folder.

The logic tests don't open any window, play audio, touch a Stream Deck or
talk to a real time server. Run them with:   python -m pytest -q
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Point DA Timer's data folder (APPDATA/DATimer) at a temp folder."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import da_timer
    da_timer.PROBLEMS.clear()
    da_timer._FAILING.clear()
    return tmp_path / "DATimer"

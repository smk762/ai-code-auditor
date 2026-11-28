"""A model whose weights are unknown or fractional is never reserved as smaller than it is."""
from __future__ import annotations

import pytest

from auditor import gpu_lease


def _config(monkeypatch, geo: dict) -> None:
    monkeypatch.setattr(gpu_lease, "_footprints", lambda: {"default_vram_mb": 6000, "models": {"m": geo}})


@pytest.mark.parametrize("geo", [
    {"overhead_mb": 300},
    {"num_layers": 40, "overhead_mb": 300},
    {"layer_size_mb": 200, "overhead_mb": 300},
    {"num_layers": 0, "layer_size_mb": 200, "overhead_mb": 300},
], ids=["no_geometry", "no_layer_size", "no_layer_count", "zero_layers"])
def test_unknown_weights_are_none_and_reserve_the_default(monkeypatch, geo) -> None:
    _config(monkeypatch, geo)
    assert gpu_lease.model_mb("m") is None
    assert gpu_lease.vram_mb("m") == 6000


def test_fractional_layer_size_is_multiplied_before_rounding_up(monkeypatch) -> None:
    _config(monkeypatch, {"num_layers": 10, "layer_size_mb": 1.95, "overhead_mb": 50})
    assert gpu_lease.model_mb("m") == 20
    assert gpu_lease.vram_mb("m") == 70


def test_a_string_layer_size_is_read_as_a_number(monkeypatch) -> None:
    _config(monkeypatch, {"num_layers": 4, "layer_size_mb": "1.5"})
    assert gpu_lease.model_mb("m") == 6

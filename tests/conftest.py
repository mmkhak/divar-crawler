"""Speed up human-browse integration tests (production still enforces 20s+ delays)."""
from __future__ import annotations

import pytest

from app import rate_policy


@pytest.fixture(autouse=True)
def fast_human_reveal_pacing(monkeypatch, request):
  if request.node.get_closest_marker("rate_policy_prod"):
    return
  def _fast_wait(row, settings):
    return 0.01

  def _fast_batch(base, **kwargs):
    return 0.0

  def _relaxed_clamp(delay_min, delay_max, **kwargs):
    lo = float(delay_min if delay_min is not None else 0.01)
    hi = float(delay_max if delay_max is not None else 0.01)
    if hi < lo:
      hi = lo
    return lo, hi

  monkeypatch.setattr(rate_policy, "MIN_PHONE_DELAY_SEC", 0.01)
  monkeypatch.setattr(rate_policy, "clamp_phone_delays", _relaxed_clamp)
  monkeypatch.setattr(rate_policy, "phone_reveal_wait", _fast_wait)
  monkeypatch.setattr(rate_policy, "batch_pause_seconds", _fast_batch)
  monkeypatch.setattr("app.runner.phone_reveal_wait", _fast_wait)
  monkeypatch.setattr("app.runner.batch_pause_seconds", _fast_batch)
  monkeypatch.setattr("app.main.clamp_phone_delays", _relaxed_clamp)
  monkeypatch.setattr("app.main.MIN_PHONE_DELAY_SEC", 0.01)
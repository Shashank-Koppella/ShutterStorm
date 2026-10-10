"""Shared fixtures: every test runs against the simulator (no Docker needed)."""
from __future__ import annotations

import pytest

from shutterstorm.checkpoint import take_checkpoint
from shutterstorm.config import Settings
from shutterstorm.context import build_context


@pytest.fixture
def ctx(tmp_path):
    """A simulated context with its own temp home folder."""
    context = build_context(Settings(simulate=True, home=tmp_path, llm_mode="off"))
    yield context
    context.close()


@pytest.fixture
def checkpoint(ctx):
    return take_checkpoint(ctx)

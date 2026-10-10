"""Journal, scope check, fault apply/undo, TTL, guard and panic, all in simulation."""
import time

import pytest

from shutterstorm.config import ML, POSTGRES, REDIS, SERVER
from shutterstorm.dockerctl import ScopeError
from shutterstorm.faults import CATALOG, FaultError, apply_fault, apply_faults, get_fault, undo_faults
from shutterstorm.load import load_running
from shutterstorm.guard import entry_is_orphaned, guard_loop
from shutterstorm.journal import Journal
from shutterstorm.safety import AbortRun, check_stop, panic


def test_journal_roundtrip_and_stop(tmp_path):
    j = Journal(tmp_path)
    entry_id = j.add("fault", "x", {"type": "unpause", "container": ML}, ttl_s=10)
    assert Journal(tmp_path).get(entry_id)["recipe"]["container"] == ML  # persisted
    j.remove(entry_id)
    assert j.is_empty()
    assert not j.stop_requested()
    j.request_stop()
    assert j.stop_requested()
    j.clear_stop()
    assert not j.stop_requested()


def test_scope_refuses_foreign_and_unknown_containers(ctx):
    with pytest.raises(ScopeError):
        ctx.docker.pause("other_team_db")          # exists, wrong compose project
    with pytest.raises(ScopeError):
        ctx.docker.stop("some_random_container")   # not in the allow-list
    ctx.sim.containers[REDIS]["project"] = "not-immich"
    with pytest.raises(ScopeError):
        ctx.docker.pause(REDIS)                    # right name, wrong label
    assert not ctx.sim.containers["other_team_db"]["paused"]


def test_unknown_fault_is_rejected():
    with pytest.raises(FaultError):
        get_fault("docker_kill_postgres")


@pytest.mark.parametrize("fault_id", list(CATALOG))
def test_every_fault_applies_and_undoes(ctx, fault_id):
    spec = CATALOG[fault_id]
    actives = apply_faults(ctx, spec)
    assert len(ctx.journal.entries()) == len(spec.atomic)  # one write-ahead entry per applied part
    ok, detail = undo_faults(ctx, actives)
    assert ok, detail
    assert not load_running(actives[0].recipe.get("load_id", "-"))
    assert ctx.journal.is_empty()
    for name in (SERVER, ML, REDIS, POSTGRES):
        c = ctx.sim.containers[name]
        assert c["running"] and not c["paused"] and c["nano"] == 0 and c["quota"] == 0
    assert not ctx.sim.netem and not ctx.sim.helpers


def test_ttl_watchdog_undoes_fault(ctx):
    active = apply_fault(ctx, CATALOG["redis_pause"], ttl_s=0.5)
    assert ctx.sim.containers[REDIS]["paused"]
    deadline = time.time() + 5
    while time.time() < deadline and not active.undone:
        time.sleep(0.05)
    assert active.ttl_fired and active.undone
    assert not ctx.sim.containers[REDIS]["paused"]
    assert ctx.journal.is_empty()


def test_guard_takes_over_when_owner_dies(ctx):
    active = apply_fault(ctx, CATALOG["ml_pause"])
    active._timer.cancel()
    entry = ctx.journal.entries()[0]
    assert entry_is_orphaned(entry, time.time()) is None           # owner alive, TTL not reached
    assert entry_is_orphaned(entry, time.time(), alive=lambda pid: False)
    assert entry_is_orphaned(entry, entry["deadline"] + 60)        # TTL passed
    undone = guard_loop(ctx, poll_s=0.05, alive=lambda pid: False, max_lifetime_s=5)
    assert undone >= 0 and ctx.journal.is_empty()
    assert not ctx.sim.containers[ML]["paused"]


def test_panic_mid_fault_restores_everything(ctx, checkpoint):
    apply_fault(ctx, CATALOG["db_stop"])
    apply_fault(ctx, CATALOG["ml_cpu_throttle"])
    ctx.docker.pause(REDIS)  # an un-journaled change: panic's generic restore must still fix it
    assert not ctx.sim.containers[POSTGRES]["running"]
    result = panic(ctx, reason="test")
    assert result["journal_left"] == 0
    for name in (SERVER, ML, REDIS, POSTGRES):
        c = ctx.sim.containers[name]
        assert c["running"] and not c["paused"] and c["nano"] == 0 and c["quota"] == 0
    with pytest.raises(AbortRun):
        check_stop(ctx)  # panic also stops any running workflow

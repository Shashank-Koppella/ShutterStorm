"""Operator approval of repair patches (human in the loop), in simulation."""
import json

from shutterstorm.approval import Approver
from shutterstorm.config import ML2, OVERRIDE_FILE
from shutterstorm.workflow import Workflow


def scripted(*answers):
    """An Approver that answers from a list and records what it showed."""
    shown, replies = [], iter(answers)
    approver = Approver("ask", ask=lambda prompt: next(replies), out=shown.append, interactive=True)
    return approver, shown


def run(ctx, approver, faults=("ml_pause",)):
    run_dir = Workflow(ctx, list(faults), repair=True, approver=approver).run()
    return json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))


def test_prompt_explains_issue_and_fix_then_applies_on_yes(ctx):
    approver, shown = scripted("y")
    data = run(ctx, approver)
    text = "\n".join(shown)
    assert "REPAIR APPROVAL NEEDED" in text
    assert "J3 smart search (ML): HUNG" in text            # the issue, from measured data
    assert "machineLearning.urls" in text                  # the exact change
    assert "immich_machine_learning_2" in text             # the side effect
    repair = data["experiments"][0]["repair"]
    assert repair["status"] == "verified"
    assert repair["attempts"][0]["approval"]["by"] == "operator"


def test_decline_asks_for_a_different_fix(ctx):
    approver, shown = scripted("n", "y")
    data = run(ctx, approver)
    attempts = data["experiments"][0]["repair"]["attempts"]
    assert attempts[0]["result"] == "declined by operator"
    assert attempts[0]["proposed"] != attempts[1]["proposed"]
    assert not (ctx.settings.immich_dir / OVERRIDE_FILE).exists()


def test_skip_applies_nothing(ctx):
    approver, _ = scripted("s")
    data = run(ctx, approver)
    assert data["experiments"][0]["repair"]["status"] == "skipped_by_operator"
    assert ML2 not in ctx.sim.containers
    assert data["cleanup"]["ok"]


def test_quit_stops_run_but_cleans_up_and_keeps_the_finding(ctx):
    approver, _ = scripted("q")
    data = run(ctx, approver, faults=("ml_pause", "redis_pause"))
    assert data["status"] == "stopped by operator"
    assert [e["label"] for e in data["experiments"]] == ["ml_pause"]
    assert data["experiments"][0]["repair"]["status"] == "operator_quit"
    assert data["cleanup"]["ok"] and ctx.journal.is_empty()


def test_no_terminal_means_not_applied(ctx):
    approver = Approver("ask", out=lambda text: None, interactive=False)
    data = run(ctx, approver)
    assert data["experiments"][0]["repair"]["status"] == "not_approved"
    assert ML2 not in ctx.sim.containers


def test_invalid_answers_are_asked_again():
    approver, shown = scripted("maybe", "", "y")
    assert approver.request("question")["decision"] == "approve"
    assert shown.count("Please answer y, n, s or q.") == 2

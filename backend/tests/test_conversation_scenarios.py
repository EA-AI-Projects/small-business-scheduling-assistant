"""The manual scenario runner uses the local simulator without requiring OpenAI in CI."""

from dataclasses import replace
from pathlib import Path

import pytest

from evals.conversation_scenarios import load_scenario, main, run_scenario
from scheduling.domain.conversation import MessageContext, MessageProposal

EXAMPLE = Path(__file__).resolve().parents[1] / "evals/scenarios/booking_request.json"


class ScriptedModel:
    def propose(self, body: str, context: MessageContext) -> MessageProposal:
        assert body == "Could you come tomorrow morning?"
        assert context.history[-1].text == body
        return MessageProposal("availability", None, None, None, False,
                               date_from="2026-09-30", date_to="2026-09-30",
                               time_from="08:00", time_to="12:00")


def test_example_runs_a_real_local_conversation_without_openai(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_scenario(load_scenario(EXAMPLE), ScriptedModel())
    output = capsys.readouterr().out
    assert "client-1 reply: Open times" in output
    assert ". owner at " in output and ": Approve " in output
    assert "{{pending_ref" not in output
    assert "'client-1': 1" in output
    assert "PASS: Client requests a booking and owner approves it" in output


def test_failed_expectation_prints_the_exchange_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scenario = load_scenario(EXAMPLE)
    wrong_last = replace(scenario.steps[-1], pending_for={"client-1": 2})
    assert not run_scenario(replace(scenario, steps=(*scenario.steps[:-1], wrong_last)),
                            ScriptedModel())
    output = capsys.readouterr().out
    assert "client-1 notification:" in output
    assert "client-1 had 0 pending requests, expected 2" in output
    assert "FAIL: Client requests a booking and owner approves it" in output


def test_live_calls_need_explicit_flag_and_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main([str(EXAMPLE), "--live"]) == 2
    assert "no model call was made" in capsys.readouterr().err
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    assert main([str(EXAMPLE)]) == 2

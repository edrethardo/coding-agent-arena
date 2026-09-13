"""ARENA_SESSION_MODE-Ueberschreibung und die Abbruchregel.

Beide entscheiden, WELCHE Runden ueberhaupt entstehen -- ein stiller Fehler hier kostet
einen halben Tag Kartenzeit und faellt erst in der Auswertung auf.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import run_game_bench as r

BENCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "benchmarks", "antifa_survivors_v3")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ARENA_SESSION_MODE", raising=False)
    monkeypatch.delenv("ARENA_STOP_ON_ABORT", raising=False)


def test_bench_json_wins_without_override():
    cfg = r.load_bench_cfg(BENCH)
    assert cfg["session_mode"] == "continue"
    assert "session_mode_override" not in cfg


def test_override_switches_to_fresh_and_records_itself(monkeypatch):
    monkeypatch.setenv("ARENA_SESSION_MODE", "fresh")
    cfg = r.load_bench_cfg(BENCH)
    assert cfg["session_mode"] == "fresh"
    # Ohne diesen Eintrag in der Provenance sind zwei Laeufe derselben Zeile spaeter
    # nicht auseinanderzuhalten.
    assert cfg["session_mode_override"] == "fresh"


def test_override_suppresses_the_session_variables(monkeypatch):
    monkeypatch.setenv("ARENA_SESSION_MODE", "fresh")
    cfg = r.load_bench_cfg(BENCH)
    assert r.session_env(cfg, {"name": "n", "uuid": "u"}, True) == {}


def test_fresh_round_carries_the_full_spec(monkeypatch, tmp_path):
    """Eine frische Sitzung je Runde darf den Agenten nicht ohne Spezifikation lassen."""
    monkeypatch.setenv("ARENA_SESSION_MODE", "fresh")
    cfg = r.load_bench_cfg(BENCH)
    base = open(os.path.join(BENCH, "base.txt"), encoding="utf-8").read()
    task, total = r.compose(BENCH, 3, str(tmp_path), continuing=True)
    assert task.startswith(base[:200])
    assert "ROUND 3" in task or "3 of %d" % total in task


def test_continue_round_is_only_the_new_instruction(tmp_path):
    task, _ = r.compose(BENCH, 3, str(tmp_path), continuing=True)
    assert task.startswith("--- ROUND 3")


def test_bad_override_is_rejected_loudly(monkeypatch):
    monkeypatch.setenv("ARENA_SESSION_MODE", "Fresh")   # Grossschreibung ist ein Tippfehler
    with pytest.raises(SystemExit):
        r.load_bench_cfg(BENCH)


@pytest.mark.parametrize("res,expected", [
    ({"exit_code": 8, "ended_by": None}, "harness failure"),
    ({"exit_code": 3, "ended_by": "timeout"}, "timeout"),
    ({"exit_code": 3, "ended_by": "stalled"}, "stalled"),
    ({"exit_code": 0, "ended_by": None}, None),
    ({"exit_code": 0, "ended_by": None, "noop": True}, None),   # No-op ist ein Ergebnis
    ({"exit_code": 4, "ended_by": None}, None),                 # Endpunkt tot: eigener Pfad
])
def test_abort_reason(res, expected):
    assert r.abort_reason(res) == expected


def test_stall_detection_survives_the_override(monkeypatch):
    """stall_s haengt an bench.json, nicht am session_mode -- sonst schaltet die
    Ueberschreibung die Stillstandserkennung stillschweigend ab."""
    monkeypatch.setenv("ARENA_SESSION_MODE", "fresh")
    assert r.stall_s_for(BENCH) == 900

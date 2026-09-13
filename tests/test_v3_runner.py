import json, os, shutil, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import run_game_bench as r

def bench_dir(tmp, cfg):
    os.makedirs(tmp, exist_ok=True)
    open(os.path.join(tmp, "base.txt"), "w").write("BASE")
    json.dump(["R1", "R2"], open(os.path.join(tmp, "rounds.json"), "w"))
    if cfg is not None: json.dump(cfg, open(os.path.join(tmp, "bench.json"), "w"))
    return tmp

V3CFG = {"paste_prior_file": False, "session_mode": "continue", "grade_repeats": 3, "window_s": [180, 300], "grader_sha256": None, "proxy_required": False}

def test_compose_without_prior_file_never_pastes_the_file():
    b = bench_dir(tempfile.mkdtemp(), V3CFG)
    w = tempfile.mkdtemp(); open(os.path.join(w, "index.html"), "w").write("<html>" * 1000)
    t1, _ = r.compose(b, 1, w); t2, _ = r.compose(b, 2, w)          # Fallback-Form: Spec + Hinweis
    assert "<html>" not in t2 and "Read what you need" in t2 and "BASE" in t2
    assert abs(len(t1) - len(t2)) < 200

def test_compose_follow_up_is_task_only():
    b = bench_dir(tempfile.mkdtemp(), V3CFG)
    w = tempfile.mkdtemp(); open(os.path.join(w, "index.html"), "w").write("<html>")
    t2, n = r.compose(b, 2, w, continuing=True)
    assert n == 2 and "BASE" not in t2 and "R2" in t2 and "same session" in t2 and len(t2) < 400

def test_session_ids_are_deterministic_and_valid():
    import uuid
    a = r.session_for("V3-20260910T010203--v3-cc-opus-5"); b = r.session_for("V3-20260910T010203--v3-cc-opus-5")
    assert a == b and uuid.UUID(a["uuid"]) and a["name"] == "arena-V3-20260910T010203--v3-cc-opus-5"
    assert r.session_for("X", fallback_round=3)["name"] == "arena-X-fresh-r3"

def test_compose_v2_behaviour_unchanged_without_bench_json():
    b = bench_dir(tempfile.mkdtemp(), None)
    w = tempfile.mkdtemp(); open(os.path.join(w, "index.html"), "w").write("<html>PRIOR</html>")
    t2, _ = r.compose(b, 2, w)
    assert "PRIOR" in t2 and "CURRENT index.html" in t2

def test_build_hash_covers_every_file():
    w = tempfile.mkdtemp(); open(os.path.join(w, "index.html"), "w").write("a")
    h1 = r.build_hash(w); open(os.path.join(w, "game.js"), "w").write("b"); h2 = r.build_hash(w)
    os.makedirs(os.path.join(w, ".hidden")); open(os.path.join(w, ".hidden", "x"), "w").write("c")
    assert h1 != h2 and r.build_hash(w) == h2

def test_median_of_three_grades():
    g = [{"gate": 5, "capability": {"passed": 10, "total": 24, "checks": {"a": True, "b": False}}, "rejected": [], "screenshots": [], "fps": 60},
         {"gate": 2, "capability": {"passed": 12, "total": 24, "checks": {"a": True, "b": True}}, "rejected": ["x"], "screenshots": [], "fps": 58},
         {"gate": 5, "capability": {"passed": 13, "total": 24, "checks": {"a": False, "b": True}}, "rejected": [], "screenshots": [], "fps": 61}]
    m = r.median_grade(g)
    assert m["gate"] == 5 and m["capability"]["checks"] == {"a": True, "b": True} and m["capability"]["passed"] == 2
    assert m["span"] == [10, 13] and m["rejected"] == ["x"] and m["fps"] == 60
    assert m["grades_failed"] == 0

def test_median_of_three_grades_failure_in_position_0_is_ignored():
    # A failed grader call ({"checks": {}, "total": 0}) sitting at index 0 used to zero out the
    # whole checklist for the run (median_grade read keys/total from grades[0]). Keys/total must
    # come from the first SUCCESSFUL entry, and majority/median/span must run over the successful
    # entries only.
    fail = {"gate": 0, "capability": {"passed": 0, "total": 0, "checks": {}}, "rejected": [], "screenshots": [], "fps": 0,
            "notes": ["grader failed: boom"]}
    ok_a = {"gate": 5, "capability": {"passed": 20, "total": 24, "checks": {"a": True, "b": False}}, "rejected": [], "screenshots": [], "fps": 60}
    ok_b = {"gate": 5, "capability": {"passed": 20, "total": 24, "checks": {"a": True, "b": True}}, "rejected": [], "screenshots": [], "fps": 61}
    m = r.median_grade([fail, ok_a, ok_b])
    assert m["gate"] == 5
    assert m["capability"]["total"] == 24
    assert m["capability"]["checks"] == {"a": True, "b": False}
    assert m["capability"]["passed"] == 1
    assert m["span"] == [20, 20]
    assert m["grades_failed"] == 1

def test_median_of_three_grades_all_failed_returns_zero_shape():
    fail = {"gate": 0, "capability": {"passed": 0, "total": 0, "checks": {}}, "rejected": [], "screenshots": [], "fps": 0}
    m = r.median_grade([fail, dict(fail), dict(fail)])
    assert m["gate"] == 0 and m["rung"] == 0
    assert m["capability"] == {"passed": 0, "total": 0, "checks": {}}
    assert m["span"] == [0, 0] and m["grades_failed"] == 3

def test_proxy_table_and_wrappers_agree():
    for row in r.PROXY:
        assert row in r.WRAPPERS
    ports = [p for (_, _, p) in r.PROXY.values()]
    assert len(ports) == len(set(ports))
    assert r.WRAPPERS["v3-cc-opus-5"][1]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{r.PROXY['v3-cc-opus-5'][2]}"

def test_fresh_mode_sets_no_session_env():
    b = bench_dir(tempfile.mkdtemp(), None)                     # no bench.json -> v2, "fresh"
    cfg = r.load_bench_cfg(b)
    env = r.session_env(cfg, r.session_for("Y"), False)
    assert not (set(env) & {"ARENA_SESSION_NAME", "ARENA_SESSION_ID", "ARENA_SESSION_CONTINUE"})

def test_continue_mode_sets_session_env():
    b = bench_dir(tempfile.mkdtemp(), V3CFG)                    # session_mode: "continue"
    cfg = r.load_bench_cfg(b)
    s = r.session_for("Z")
    env1 = r.session_env(cfg, s, False)                         # round 1
    env2 = r.session_env(cfg, s, True)                          # round 2
    assert env1["ARENA_SESSION_NAME"] == s["name"] == env2["ARENA_SESSION_NAME"]
    assert env1["ARENA_SESSION_ID"] == s["uuid"] == env2["ARENA_SESSION_ID"]
    assert env1["ARENA_SESSION_CONTINUE"] == "0" and env2["ARENA_SESSION_CONTINUE"] == "1"

def test_stall_disabled_by_default_for_v2_bench_without_json():
    b = bench_dir(tempfile.mkdtemp(), None)
    saved = os.environ.pop("ARENA_STALL_S", None)
    try:
        assert r.stall_s_for(b) == 0
    finally:
        if saved is not None: os.environ["ARENA_STALL_S"] = saved

def test_stall_enabled_by_default_for_v3_bench_with_json():
    b = bench_dir(tempfile.mkdtemp(), V3CFG)
    saved = os.environ.pop("ARENA_STALL_S", None)
    try:
        assert r.stall_s_for(b) == 900
    finally:
        if saved is not None: os.environ["ARENA_STALL_S"] = saved

def test_stall_env_override_applies_even_without_bench_json():
    b = bench_dir(tempfile.mkdtemp(), None)
    saved = os.environ.get("ARENA_STALL_S")
    os.environ["ARENA_STALL_S"] = "42"
    try:
        assert r.stall_s_for(b) == 42
    finally:
        if saved is None: os.environ.pop("ARENA_STALL_S", None)
        else: os.environ["ARENA_STALL_S"] = saved

def _reap(p):
    try: p.kill()
    except Exception: pass
    try: p.wait()
    except Exception: pass

def test_wait_round_kills_a_stalled_agent():
    import subprocess, time
    w = tempfile.mkdtemp()
    p = subprocess.Popen(["sleep", "300"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        t0 = time.time(); rc, out, why, _fw = r.wait_round(p, "task", 0, w, None, stall_s=2, poll_s=1)
        assert rc == 3 and why == "stalled" and out == "" and time.time() - t0 < 10
    finally:
        _reap(p); shutil.rmtree(w, ignore_errors=True)

def test_wait_round_returns_agent_output_and_rc():
    import subprocess
    w = tempfile.mkdtemp()
    p = subprocess.Popen(["bash", "-c", "cat >/dev/null; echo hello; exit 8"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        rc, out, why, _fw = r.wait_round(p, "task", 0, w, None, stall_s=60, poll_s=1)
        assert rc == 8 and out.strip() == "hello" and why is None
    finally:
        _reap(p); shutil.rmtree(w, ignore_errors=True)

def test_wait_round_survives_exit_before_reading_stdin():
    # hermes-task-model exit 4 (logged-out provider), claude-code-task exit 3/4 (bad dir/binary),
    # and Task 5b's exit 9 (lost session) all exit WITHOUT ever reading stdin. Writing stdin on
    # the main thread raised BrokenPipeError there, which run_round's generic except turned into
    # rc 5 "runner error" -- silently defeating the exit-9 fallback and the exit-4 stop.
    import subprocess
    w = tempfile.mkdtemp()
    p = subprocess.Popen(["bash", "-c", "exit 9"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        rc, out, why, _fw = r.wait_round(p, "task", 0, w, None, stall_s=60, poll_s=1)
        assert rc == 9 and why is None and out == ""
    finally:
        _reap(p); shutil.rmtree(w, ignore_errors=True)

def test_wait_round_handles_a_large_task_without_deadlock():
    # A v2 prompt with the pasted prior file can exceed the 64 KB pipe buffer; writing it on the
    # main thread would block until the agent drains it, outside the stall/timeout loop.
    import subprocess
    w = tempfile.mkdtemp()
    big = "x" * (200 * 1024)
    p = subprocess.Popen([sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        rc, out, why, _fw = r.wait_round(p, big, 0, w, None, stall_s=60, poll_s=1)
        assert rc == 0 and out.strip() == str(len(big)) and why is None
    finally:
        _reap(p); shutil.rmtree(w, ignore_errors=True)

def test_wait_round_records_the_first_file_write():
    """Spec 6: der Ereignisstreifen braucht "erstes Schreiben einer Datei" als Marker
    (Quelle: Runner, mtime im Arbeitsverzeichnis). Vorher wurde nichts aufgezeichnet."""
    import subprocess, time
    w = tempfile.mkdtemp()
    p = subprocess.Popen(["bash", "-c", f"cat >/dev/null; sleep 1.5; touch {w}/index.html; sleep 1.5"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    t0 = time.time()
    try:
        rc, out, why, first_write_t = r.wait_round(p, "task", 0, w, None, stall_s=60, poll_s=1)
        assert rc == 0 and why is None
        assert first_write_t is not None and t0 <= first_write_t <= time.time()
    finally:
        _reap(p); shutil.rmtree(w, ignore_errors=True)

def test_wait_round_reports_no_write_when_nothing_is_written():
    import subprocess
    w = tempfile.mkdtemp()
    p = subprocess.Popen(["bash", "-c", "cat >/dev/null; sleep 1.2"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        rc, out, why, first_write_t = r.wait_round(p, "task", 0, w, None, stall_s=60, poll_s=1)
        assert rc == 0 and first_write_t is None
    finally:
        _reap(p); shutil.rmtree(w, ignore_errors=True)

def _grader_module():
    import importlib.util
    p = os.path.join(os.path.dirname(__file__), "..", "benchmarks", "antifa_survivors_v3", "grader.py")
    spec = importlib.util.spec_from_file_location("v3_grader", p)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_grader_harness_error_is_not_twenty_four_failed_checks(capsys, monkeypatch, tmp_path):
    """C2: ein Grader, der GAR NICHT laufen konnte (geckodriver startet nicht, Seite laedt nie,
    Sitzung tot), meldete capability {passed 0, total 24, checks: 24x False}. median_grade behielt
    diesen Eintrag (checks ist truthy), zog den Median der Runde herunter und vergiftete
    span=[0,N] und damit die Podium-Regel -- zwei solche Aussetzer druecken Gate und Checkliste
    auf 0, ohne dass irgendwo steht, dass der Browser schuld war und nicht der Build."""
    g = _grader_module()
    (tmp_path / "index.html").write_text("<canvas></canvas>")
    def boom(*a, **k): raise RuntimeError("geckodriver would not spawn")
    monkeypatch.setattr(g, "score", boom)
    monkeypatch.setattr(g, "DRIVER", __file__)          # existiert -> main() bricht nicht vorher ab
    monkeypatch.setattr(sys, "argv", ["grader.py", str(tmp_path), "--json", "--seed", "s"])
    g.main()
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["capability"] == {"passed": 0, "total": 0, "checks": {}}
    assert out["notes"][0].startswith("harness error")
    # und median_grade wirft ihn genau deshalb hinaus
    ok = {"gate": 5, "capability": {"passed": 24, "total": 24, "checks": {"a": True}},
          "rejected": [], "screenshots": [], "fps": 60}
    m = r.median_grade([out, ok, dict(ok)])
    assert m["gate"] == 5 and m["span"] == [24, 24] and m["grades_failed"] == 1

def test_grader_missing_index_html_stays_a_real_zero(capsys, monkeypatch, tmp_path):
    """Die Gegenprobe zu C2: ein Build ohne index.html IST bewertet worden -- 0 von 24, zu Recht
    -- und muss im Median bleiben."""
    g = _grader_module()
    monkeypatch.setattr(sys, "argv", ["grader.py", str(tmp_path), "--json", "--seed", "s"])
    g.main()
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["capability"]["total"] == len(g.CHECKS)
    assert out["capability"]["passed"] == 0 and out["notes"] == ["no index.html"]
    ok = {"gate": 5, "capability": {"passed": 24, "total": 24, "checks": {k: True for k in g.CHECKS}},
          "rejected": [], "screenshots": [], "fps": 60}
    m = r.median_grade([out, ok, dict(ok)])
    assert m["grades_failed"] == 0 and m["span"] == [0, 24]


# --- Fliessband: der Agent der naechsten Runde laeuft, waehrend r<n> bewertet wird -------------
#
# Kein Browser, kein Netz, kein Phoenix: der Agent ist ein Stand-in, der eine Datei schreibt und
# endet, der Grader ein Stand-in (ARENA_GRADER_BIN), der schlaeft und eine feste JSON-Zeile
# druckt. Beide schreiben ihre Ereignisse mit Zeitstempel in eine Markerdatei -- die Reihenfolge
# DIESER Zeilen ist genau die Eigenschaft, um die es geht.

AGENT_STANDIN = '''import os, sys, time
log, wd = os.environ["ARENA_TEST_LOG"], sys.argv[1]
open(log, "a").write("agent-start %.4f continue=%s\\n"
                     % (time.time(), os.environ.get("ARENA_SESSION_CONTINUE", "-")))
task = sys.stdin.read()
p = os.path.join(wd, "index.html")
prior = open(p).read() if os.path.exists(p) else ""
open(p, "w").write(prior + "<p>%d</p>" % len(task))
time.sleep(0.3)          # lang genug, dass ein Ueberlappen mit der Bewertung sichtbar wird
open(log, "a").write("agent-end %.4f -\\n" % time.time())
'''

GRADER_STANDIN = '''import json, os, sys, time
log, wd = os.environ["ARENA_TEST_LOG"], sys.argv[1]
open(log, "a").write("grade-start %.4f %s\\n" % (time.time(), os.path.basename(wd)))
time.sleep(float(os.environ.get("ARENA_STUB_GRADE_S", "0")))
open(log, "a").write("grade-end %.4f %s\\n" % (time.time(), os.path.basename(wd)))
print(json.dumps({"gate": 3, "rung": 3, "seed": "s", "window_s": 180, "fps": 60,
                  "capability": {"passed": 7, "total": 24, "checks": {"a": True, "b": False}},
                  "rejected": [], "screenshots": [], "notes": ["gate stops at 3"]}))
'''


def _runner_fixture(tmp_path, monkeypatch, cfg, grade_s="1"):
    """Ein vollstaendiger Mini-Lauf, ganz in tmp_path: eigenes HERE (Bank + Wrapper), eigenes
    WORK_ROOT, Stand-in-Agent, Stand-in-Grader. Gibt (game, workdir, run_dir, log) zurueck."""
    home = str(tmp_path / "repo"); game = "fakegame"
    bench = bench_dir(os.path.join(home, "benchmarks", game), cfg)
    agent = os.path.join(home, "stand-in-agent.py"); open(agent, "w").write(AGENT_STANDIN)
    grader = os.path.join(home, "stand-in-grader.py"); open(grader, "w").write(GRADER_STANDIN)
    log = str(tmp_path / "events.log"); open(log, "w").close()
    monkeypatch.setenv("ARENA_TEST_LOG", log)
    monkeypatch.setenv("ARENA_GRADER_BIN", grader)
    monkeypatch.setenv("ARENA_STUB_GRADE_S", grade_s)
    # NIE der echte Grader-Lock: sonst wartet der Test auf die Bewertungen einer laufenden
    # Kampagne und haelt danach deren neun Zeilen auf.
    monkeypatch.setenv("ARENA_GRADER_LOCK", str(tmp_path / "grader.lock"))
    monkeypatch.setattr(r, "HERE", home)
    monkeypatch.setattr(r, "RUN_ID", "TEST")
    monkeypatch.setattr(r, "WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(r, "WRAPPERS", {"test-row": (agent, {}, "stub", "test-model", "remote")})
    monkeypatch.setattr(r, "provenance", lambda *a, **k: {"note": "test"})
    monkeypatch.setattr(r.arena_trace, "setup", lambda *a, **k: _NoTracing())
    monkeypatch.setattr(r.agent_sandbox, "argv", lambda w, wd: [sys.executable, w, wd])
    return (game, os.path.join(str(tmp_path / "work"), game, "test-row"),
            os.path.join(bench, "runs", "TEST"), log)


class _NoTracing:
    def force_flush(self, **kw): return True


def _run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["run_game_bench.py"] + list(argv))
    r.main()


def _events(log):
    out = []
    for line in open(log):
        kind, t, what = line.split()
        out.append((kind, float(t), what))
    return out


def _results(run_dir):
    return json.load(open(os.path.join(run_dir, "results_test-row.json")))["rounds"]


AGENT_KEYS = ("round", "run_id", "build_sha256_16", "noop", "session_id", "files", "started",
              "exit_code", "exit_meaning", "wall_s", "task_chars", "stdout_chars", "proxy")
GRADE_KEYS = ("ladder_rung", "ladder_note", "capability", "span", "grades", "grades_failed",
              "rejected", "screenshots", "grader_fps")


def test_round_two_starts_while_round_one_is_still_being_graded(tmp_path, monkeypatch):
    """Der Kern der Umstellung: der Grader ist maschinenweit serialisiert, der Agent muss
    trotzdem weiterlaufen. Runde 2 startet, WAEHREND r1 noch bewertet wird."""
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG, grade_s="1")
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "2")
    ev = _events(log)
    starts = [t for k, t, _ in ev if k == "agent-start"]
    r1_graded = [t for k, t, w in ev if k == "grade-end" and w == "r1"]
    assert len(starts) == 2 and len(r1_graded) == V3CFG["grade_repeats"]
    assert starts[1] < max(r1_graded), "round 2's agent waited for round 1's grading"
    # und die beiden ueberlappten wirklich: r1 wurde schon bewertet, als Runde 2 noch lief
    ends = [t for k, t, _ in ev if k == "agent-end"]
    assert min(t for k, t, w in ev if k == "grade-start" and w == "r1") < ends[1]
    # bewertet wird der Schnappschuss, nicht das Arbeitsverzeichnis
    assert {w for k, _, w in ev if k == "grade-start"} == {"r1", "r2"}

    rounds = _results(run_dir)
    assert [x["round"] for x in rounds] == [1, 2]
    for x in rounds:
        assert all(x.get(k) is not None for k in AGENT_KEYS if k != "proxy")
        assert all(x.get(k) is not None for k in GRADE_KEYS)
        assert x["exit_code"] == 0 and x["ladder_rung"] == 3
        assert x["capability"]["passed"] == 1 and len(x["grades"]) == V3CFG["grade_repeats"]
        assert x["files"] == ["index.html"]
    assert rounds[0]["build_sha256_16"] != rounds[1]["build_sha256_16"]
    assert os.path.exists(os.path.join(run_dir, "r1", "index.html"))


def test_v2_bench_without_bench_json_still_grades_before_the_next_round(tmp_path, monkeypatch):
    """Gegenprobe: eine v2-Bank (keine bench.json) behaelt die alte Reihenfolge -- ihre Zahlen
    sind unter genau dieser Reihenfolge entstanden."""
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, None, grade_s="1")
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "2")
    ev = _events(log)
    starts = [t for k, t, _ in ev if k == "agent-start"]
    r1_graded = [t for k, t, w in ev if k == "grade-end" and w == "r1"]
    assert len(starts) == 2 and len(r1_graded) == 1        # grade_repeats 1, wie v2
    assert r1_graded[0] < starts[1], "v2 must grade round 1 before round 2's agent starts"
    rounds = _results(run_dir)
    assert [x["round"] for x in rounds] == [1, 2] and rounds[0]["ladder_rung"] == 3


def test_resume_after_agent_snapshots_grades_and_continues(tmp_path, monkeypatch):
    """--resume-after-agent 1: der Build im Arbeitsverzeichnis IST Runde 1. Er wird zu r1,
    bewertet, und Runde 2 laeuft in derselben Sitzung weiter (ARENA_SESSION_CONTINUE=1)."""
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG, grade_s="0")
    os.makedirs(workdir); open(os.path.join(workdir, "index.html"), "w").write("<p>round one</p>")
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "2",
              "--resume-after-agent", "1")
    assert open(os.path.join(run_dir, "r1", "index.html")).read() == "<p>round one</p>"
    rounds = _results(run_dir)
    assert [x["round"] for x in rounds] == [1, 2]
    assert rounds[0]["ladder_rung"] == 3 and rounds[0]["files"] == ["index.html"]
    assert rounds[0]["exit_code"] is None and rounds[0]["resumed"]
    assert rounds[0]["proxy"] == {"note": "not captured (resumed)"}
    assert rounds[1]["exit_code"] == 0 and rounds[1]["session_continued"] is True
    ev = _events(log)
    assert [k for k, _, _ in ev].count("agent-start") == 1          # Runde 1 wurde NICHT neu gebaut
    assert [w for k, _, w in ev if k == "agent-start"] == ["continue=1"]
    assert {w for k, _, w in ev if k == "grade-start"} == {"r1", "r2"}


def test_resume_after_agent_refuses_fresh(tmp_path, monkeypatch, capsys):
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG)
    import pytest
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, "--config", "test-row", "--game", game,
                  "--resume-after-agent", "1", "--fresh")
    assert exc.value.code == 2 and "pick one" in capsys.readouterr().err


def test_resume_grades_a_snapshot_whose_grading_was_interrupted(tmp_path, monkeypatch):
    """Ein Stopp mitten in der Bewertung laesst r<k> ohne Ergebnis zurueck. Jeder Wiedereinstieg
    holt das nach -- sonst fehlt die Runde in der Datei, obwohl ihr Build da ist."""
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG, grade_s="0")
    os.makedirs(os.path.join(run_dir, "r1"))
    open(os.path.join(run_dir, "r1", "index.html"), "w").write("<p>built, never graded</p>")
    json.dump({"calls": 4}, open(os.path.join(run_dir, "r1", "proxy.json"), "w"))
    os.makedirs(workdir); open(os.path.join(workdir, "index.html"), "w").write("<p>round two</p>")
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "2",
              "--resume-after-agent", "2")
    rounds = _results(run_dir)
    assert [x["round"] for x in rounds] == [1, 2]
    assert rounds[0]["ladder_rung"] == 3 and rounds[0]["proxy"] == {"calls": 4}
    assert rounds[0]["files"] == ["index.html"]        # proxy.json ist kein Build-Artefakt
    assert [k for k, _, _ in _events(log)].count("agent-start") == 0


def test_resume_ignores_an_empty_round_directory(tmp_path, monkeypatch):
    """r<k>/ entsteht schon VOR dem Agentenlauf. Ein Wiedereinstieg, der nur auf das
    Verzeichnis schaut, bewertet ein leeres und schreibt eine erfundene Runde mit Gate 0 in
    die Ergebnisdatei -- ein Befund, den nie jemand gemessen hat. Verlangt wird index.html."""
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG, grade_s="0")
    os.makedirs(os.path.join(run_dir, "r1"))                      # angelegt, nie gebaut
    os.makedirs(workdir); open(os.path.join(workdir, "index.html"), "w").write("<p>two</p>")
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "2",
              "--resume-after-agent", "2")
    rounds = _results(run_dir)
    assert [x["round"] for x in rounds] == [2]
    assert {w for k, _, w in _events(log) if k == "grade-start"} == {"r2"}


def test_an_interrupted_round_leaves_an_empty_directory_not_a_stale_build(tmp_path, monkeypatch):
    """Der zweite Anlauf einer Runde leert r<n> VOR dem Agentenlauf. Sonst stuende dort nach
    einem Abbruch der Build des ersten Anlaufs, und ein Wiedereinstieg wuerde ihn als Ergebnis
    dieser Runde bewerten."""
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG, grade_s="0")
    os.makedirs(os.path.join(run_dir, "r1"))
    open(os.path.join(run_dir, "r1", "index.html"), "w").write("<p>from an earlier attempt</p>")
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "1")
    assert "earlier attempt" not in open(os.path.join(run_dir, "r1", "index.html")).read()
    assert _results(run_dir)[0]["files"] == ["index.html"]


def test_the_grader_runs_in_its_own_session_and_can_be_killed(tmp_path, monkeypatch):
    """Kritisch: der Bewertungsthread ist ein daemon, ein Ctrl-C erreicht ihn nie. Der Grader
    muss deshalb in EIGENER Prozessgruppe laufen (nie in der des Runners -- die teilt er sich
    mit arena-contest und allen Zeilen) und ueber den Griff des Aufrufers erreichbar sein."""
    import subprocess, time
    # grade_repeats 1: sonst startet grade() nach dem getoeteten Grader sofort den naechsten,
    # und der Test laesst bei jedem Lauf einen schlafenden Prozess zurueck.
    b = bench_dir(str(tmp_path / "bench"), dict(V3CFG, grade_repeats=1))
    grader = str(tmp_path / "sleeper.py")
    open(grader, "w").write("import time; time.sleep(120)\n")
    wd = str(tmp_path / "wd"); os.makedirs(wd); open(os.path.join(wd, "index.html"), "w").write("x")
    monkeypatch.setenv("ARENA_GRADER_BIN", grader)
    monkeypatch.setenv("ARENA_GRADER_LOCK", str(tmp_path / "grader.lock"))
    seen = []
    th = __import__("threading").Thread(
        target=lambda: r.grade(wd, b, str(tmp_path), 1, r.load_bench_cfg(b), on_start=seen.append),
        daemon=True)
    th.start()
    for _ in range(100):
        if seen: break
        time.sleep(0.05)
    assert seen, "the grader handle never reached the caller"
    proc = seen[0]
    assert os.getpgid(proc.pid) != os.getpgid(os.getpid())      # eigene Gruppe, nicht unsere
    os.killpg(os.getpgid(proc.pid), __import__("signal").SIGKILL)
    proc.wait(10); th.join(10)


# --- Signale: das finally muss ERREICHBAR sein --------------------------------------------
#
# Live gemessen (2026-09-10): `kill -INT` an eine laufende Zeile tat nichts. Die Zeilen laufen
# als Hintergrundjob einer nicht-interaktiven Shell (`nohup ... &`), POSIX startet asynchrone
# Kommandos mit SIGINT auf SIG_IGN, und CPython uebernimmt ein geerbtes SIG_IGN. Kein
# KeyboardInterrupt, kein finally -- also weder Grader- noch Proxy-Abbau.

SIGNAL_STANDIN = """import os, signal, sys, time
sys.path.insert(0, "__REPO__")
import run_game_bench as r
r.install_signal_handlers()
marker = sys.argv[1]
armed = signal.getsignal(signal.SIGINT) is signal.default_int_handler
open(marker, "a").write("armed " + str(armed) + chr(10))
try:
    time.sleep(60)
except KeyboardInterrupt:
    open(marker, "a").write("teardown" + chr(10))
"""


def _standin_script(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = str(tmp_path / "signal_standin.py")
    open(path, "w").write(SIGNAL_STANDIN.replace("__REPO__", repo))
    return path


def _background_job(tmp_path, script):
    """Wie bin/arena-contest startet: `... &` in einer nicht-interaktiven Shell. Genau dieser
    Start ist es, der SIGINT auf SIG_IGN setzt -- ein direkter Popen tut es NICHT und wuerde
    den Fehler nicht reproduzieren."""
    import subprocess, time
    marker = str(tmp_path / "signal-marker.txt")
    # Ausgabe des Hintergrundjobs ins Nichts, wie `nohup ... > runner.log` in arena-contest:
    # erbt der Job die Pipe von capture_output, wartet subprocess.run auf sein EOF -- also auf
    # den Job, den es gerade in den Hintergrund geschickt hat.
    out = subprocess.run(
        ["bash", "-c", f'{sys.executable} {script} {marker} >/dev/null 2>&1 & echo $!'],
        capture_output=True, text=True, check=True)
    pid = int(out.stdout.strip())
    for _ in range(200):
        if os.path.exists(marker): break
        time.sleep(0.05)
    return pid, marker


def _await_exit(pid, marker, sig):
    import time
    os.kill(pid, sig)
    for _ in range(200):
        try:
            if os.waitpid(pid, os.WNOHANG) != (0, 0): break
        except ChildProcessError:                     # nicht unser Kind: die Shell hat es
            try: os.kill(pid, 0)
            except ProcessLookupError: break
        time.sleep(0.05)
    return open(marker).read()


def test_sigint_reaches_the_finally_even_as_a_background_job(tmp_path):
    import signal as sg
    script = _standin_script(tmp_path)
    pid, marker = _background_job(tmp_path, script)
    try:
        assert "armed True" in open(marker).read()    # SIG_IGN wurde ueberschrieben
        assert "teardown" in _await_exit(pid, marker, sg.SIGINT)
    finally:
        try: os.kill(pid, sg.SIGKILL)
        except ProcessLookupError: pass


def test_sigterm_also_reaches_the_finally(tmp_path):
    import signal as sg
    script = _standin_script(tmp_path)
    pid, marker = _background_job(tmp_path, script)
    try:
        assert "teardown" in _await_exit(pid, marker, sg.SIGTERM)
    finally:
        try: os.kill(pid, sg.SIGKILL)
        except ProcessLookupError: pass


def test_install_signal_handlers_sets_both_dispositions():
    import signal as sg
    old_int, old_term = sg.getsignal(sg.SIGINT), sg.getsignal(sg.SIGTERM)
    try:
        sg.signal(sg.SIGINT, sg.SIG_IGN)              # wie von der Shell geerbt
        r.install_signal_handlers()
        assert sg.getsignal(sg.SIGINT) is sg.default_int_handler
        import pytest
        with pytest.raises(KeyboardInterrupt):
            sg.getsignal(sg.SIGTERM)(sg.SIGTERM, None)
    finally:
        sg.signal(sg.SIGINT, old_int); sg.signal(sg.SIGTERM, old_term)


# --- Deckel je Bewertung ------------------------------------------------------------------
#
# Live gemessen (2026-09-10, cc-haiku r2): jede der drei Bewertungen brauchte ~26 Minuten bei
# einem Fenster von 200-290 s -- der Grader stand ~20 min in einem WebDriver-Aufruf, bevor die
# Sitzung ueberhaupt begann. Und zweimal ueberlebte ein headless Firefox seinen Grader mit
# 98 % CPU. Der Grund liegt im Browser; der Deckel gehoert in den Runner.

HANG_GRADER = '''import json, os, subprocess, sys, time
log, count = os.environ["ARENA_TEST_LOG"], os.environ["ARENA_STUB_COUNTER"]
n = 0
if os.path.exists(count):
    n = int(open(count).read() or 0)
open(count, "w").write(str(n + 1))
if n == 0:                       # nur der ERSTE Aufruf haengt
    # trap "" INT: das Kind ueberlebt das SIGINT an die Gruppe -- so wie ein Firefox, der
    # seinen geckodriver ueberlebt. Nur das SIGKILL des Nachraeumens holt es.
    child = subprocess.Popen(["bash", "-c", \'trap "" INT; sleep 300\'])
    open(log, "a").write("hang-child %.4f %d\\n" % (time.time(), child.pid))
    time.sleep(300)
    sys.exit(0)
open(log, "a").write("grade-end %.4f r%s\\n" % (time.time(), "1"))
print(json.dumps({"gate": 3, "rung": 3, "seed": "s", "window_s": 180, "fps": 60,
                  "capability": {"passed": 7, "total": 24, "checks": {"a": True, "b": False}},
                  "rejected": [], "screenshots": [], "notes": ["gate stops at 3"]}))
'''


def test_a_hanging_grading_is_bounded_and_takes_nothing_with_it(tmp_path, monkeypatch, capsys):
    import time
    game, workdir, run_dir, log = _runner_fixture(tmp_path, monkeypatch, V3CFG, grade_s="0")
    hang = str(tmp_path / "hang-grader.py"); open(hang, "w").write(HANG_GRADER)
    monkeypatch.setenv("ARENA_GRADER_BIN", hang)
    monkeypatch.setenv("ARENA_GRADE_TIMEOUT_S", "2")
    monkeypatch.setenv("ARENA_STUB_COUNTER", str(tmp_path / "count.txt"))
    _run_main(monkeypatch, "--config", "test-row", "--game", game, "--rounds", "1")

    rounds = _results(run_dir)
    assert len(rounds) == 1
    # die eine haengende Bewertung zaehlt als gescheitert und verduennt den Median NICHT
    assert rounds[0]["grades_failed"] == 1
    assert rounds[0]["ladder_rung"] == 3 and rounds[0]["capability"]["passed"] == 1
    assert "grader timeout after 2s" in rounds[0]["ladder_note"]
    assert "timeout after 2 s" in capsys.readouterr().out

    # und das Kind des Graders (der "Firefox") hat ihn nicht ueberlebt
    child = int([w for k, _, w in _events(log) if k == "hang-child"][0])
    for _ in range(100):
        try: os.kill(child, 0)
        except ProcessLookupError: break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        raise AssertionError(f"the grader's child {child} outlived it")


def test_grade_timeout_is_off_when_set_to_zero(tmp_path, monkeypatch):
    """0 heisst kein Deckel -- ein Bank-Eigentuemer, der ein sehr langes Fenster misst, soll
    ihn abschalten koennen, ohne den Code zu aendern."""
    b = bench_dir(str(tmp_path / "bench"), dict(V3CFG, grade_repeats=1))
    grader = str(tmp_path / "quick.py")
    open(grader, "w").write('import json; print(json.dumps({"gate": 1, "rung": 1}))\n')
    wd = str(tmp_path / "wd"); os.makedirs(wd); open(os.path.join(wd, "index.html"), "w").write("x")
    monkeypatch.setenv("ARENA_GRADER_BIN", grader)
    monkeypatch.setenv("ARENA_GRADER_LOCK", str(tmp_path / "grader.lock"))
    monkeypatch.setenv("ARENA_GRADE_TIMEOUT_S", "0")
    assert r.grade(wd, b, str(tmp_path), 1, r.load_bench_cfg(b))["gate"] == 1

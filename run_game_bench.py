"""Multi-round game-build benchmark: one agent row, one game, N rounds.

The artefact is graded, not the chat -- same principle as run_agentic.py. Each round hands
the agent the task plus whatever it built last round, so the context grows on purpose: that
growth is the measurement, and it is what separates serving configs that can hold a large
file in context from ones that tear.

Checkpoints after every round. A shared GPU makes no promises about how long a lock lasts,
so a preemption must cost one round, not the run.

    python run_game_bench.py --config hermes-qwen --game antifa_survivors --rounds 3
"""
import argparse
import fcntl
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

import agent_sandbox
import arena_trace
import vllm_metrics

RUN_ID = os.environ.get("ARENA_RUN_ID") or time.strftime("%Y%m%dT%H%M%S")

HERE = os.path.dirname(os.path.abspath(__file__))

# The agent's working directory MUST live outside this repo. When it sat under
# benchmarks/, the agent walked up from its cwd, found ladder_check.py and rounds.json,
# and ran the grader against the repo root in a loop instead of building the game. An
# agent that can read the scoring code can also satisfy it directly -- fake a
# window.__state with a high kill count and it scores top rung with no game behind it.
# Isolation is what makes the score mean anything.
WORK_ROOT = os.environ.get("ARENA_WORK_ROOT", os.path.expanduser("~/arena-work"))


def load_bench_cfg(bench):
    """bench.json ist optional: ohne sie verhaelt sich der Runner wie fuer v2.

    ARENA_SESSION_MODE ueberschreibt session_mode fuer EINEN Lauf, ohne die bench.json
    anzufassen -- die gilt fuer alle v3-Zeilen, und ein Vergleichslauf darf die anderen
    nicht mitverstellen. Gedacht fuer genau eine Frage: Traegt eine frische Sitzung je
    Runde weiter als eine fortgesetzte, wenn die fortgesetzte an der Kompaktierung
    stirbt? Bei "fresh" bekommt der Agent je Runde die volle Spezifikation plus den
    Rundenauftrag plus den Hinweis, dass seine Dateien im Arbeitsverzeichnis liegen
    (compose() macht das schon, siehe dort) -- er verliert also Gespraechsverlauf, nicht
    Arbeit. Ein Tippfehler faellt hier auf und nicht erst am Verhalten: alles ausser
    "fresh" und "continue" ist ein Fehler.
    """
    p = os.path.join(bench, "bench.json")
    cfg = {"paste_prior_file": True, "session_mode": "fresh", "grade_repeats": 1, "window_s": None,
           "grader_sha256": None, "proxy_required": False}
    if os.path.exists(p):
        cfg.update(json.load(open(p, encoding="utf-8")))
    override = os.environ.get("ARENA_SESSION_MODE")
    if override:
        if override not in ("fresh", "continue"):
            raise SystemExit(f"ARENA_SESSION_MODE={override!r} -- allowed: fresh, continue")
        cfg["session_mode"] = override
        cfg["session_mode_override"] = override
    return cfg


def build_files(workdir, snapshot=False):
    """Die sichtbaren Dateien eines Builds, in der Reihenfolge, in der build_hash sie hasht.

    snapshot=True blendet zusaetzlich aus, was der RUNNER in einen Schnappschuss r<n>/ legt:
    proxy.json und die Screenshot-Ordner grade-<k>/. Sonst behauptet eine nachbewertete Runde,
    der Agent haette sie geschrieben, und ihr Build-Hash haengt daran, ob schon bewertet wurde.
    """
    out = []
    for root, dirs, files in os.walk(workdir):
        # node_modules ist nie Teil des Builds. Ein Agent darf sich ein Testwerkzeug
        # installieren (die 131k-Zeile hat jsdom geholt: 27 MB, 1830 Dateien), aber die
        # Auslieferung besteht laut Spezifikation aus index.html und eigenen Skripten --
        # keine Bauwerkzeuge, keine Netzabrufe zur Laufzeit. Zaehlte es mit, bestuende die
        # Dateiliste der Runde aus 1830 fremden Dateien, und build_hash haenge vor allem
        # daran, welche Paketversion npm gerade aufgeloest hat: eine Runde, die nur ein
        # Paket nachinstalliert, saehe dann aus wie eine geaenderte Runde, und eine, die
        # nur den Build aendert, ginge im Rauschen unter.
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d != "node_modules"
                         and not (snapshot and d.startswith("grade-")))
        for f in sorted(files):
            if f.startswith("."): continue
            if snapshot and root == workdir and f == "proxy.json": continue
            out.append(os.path.relpath(os.path.join(root, f), workdir))
    return out


def build_hash(workdir, snapshot=False):
    """SHA-256 ueber ALLE sichtbaren Dateien (Pfad + Inhalt), nicht nur index.html: v3 erlaubt
    mehrere Dateien, und eine neue game.js ohne Aenderung an index.html ist Arbeit."""
    import hashlib
    h = hashlib.sha256()
    for rel in build_files(workdir, snapshot):
        h.update(rel.encode()); h.update(b"\0")
        h.update(open(os.path.join(workdir, rel), "rb").read()); h.update(b"\0")
    return h.hexdigest()[:16]


# row -> (wrapper path, extra env, harness, model, backend)
#
# `backend` says which scarce resource the row consumes, and it is load-bearing for
# parallel runs:
#   "local"  -- the 3090 in local GPU host. Exactly one such row may run at a time: the
#              vLLM counters are server-wide, so a second local row silently inflates
#              every model-side number of the first, and both records become worthless.
#   "remote" -- a hosted API. Costs no GPU, so any number of these may run alongside each
#              other and alongside one local row. A remote row must NEVER scrape the vLLM
#              metrics endpoint: :8000 is socket-activated and a bare request there wakes
#              the engine and takes ~23 GB of VRAM from whatever else is on the card.
WRAPPERS = {
    "hermes-qwen":   ("bin/hermes-task",     {}, "hermes",   "Qwen3.8-27B-Instruct", "local"),
    "opencode-qwen": ("opencode-task",       {}, "opencode", "Qwen3.8-27B-Instruct", "local"),
    # bin/dsh-task-model, not the bare name "dsh-task": that launcher lived in ~/.local/bin
    # and vanished together with the dsh install. The repo copy is a verbatim copy of it
    # plus one additive env switch, and with DSH_TASK_BACKEND unset it takes exactly the
    # local-model path the original took.
    "dsh-qwen":      ("bin/dsh-task-model",  {}, "dsh",      "Qwen3.8-27B-Instruct", "local"),
    # Pi Agent Harness (earendil-works/pi, MIT) on the same local model as hermes-qwen:
    # a third harness against one model, which is the only way to separate "the model
    # could not do it" from "the harness could not carry it" -- the question run G left
    # open when hermes exhausted its context in round 2.
    "pi-qwen":       ("bin/pi-task",          {}, "pi",       "Qwen3.8-27B-Instruct", "local"),
    # Zwei Varianten derselben Zeile, um die beiden Ursachen zu trennen, die in
    # `hermes-qwen` vermischt sind. Beide fahren ueber eine EIGENE Hermes-Wurzel
    # (HERMES_HOME), damit die Arbeitsinstallation des Nutzers unberuehrt bleibt und die
    # gepinnte Zeile `hermes-qwen` weiter das bedeutet, was ihre Zahlen behaupten.
    #   ctx131072: nur das Fenster groesser -- verlangt zusaetzlich einen Server mit
    #              max_model_len=131072, sonst misst es nichts (model.context_length ist
    #              ein Override, siehe Bericht 3.6).
    #   prune28k:  gleiches Fenster, andere Kontextfuehrung -- proactive_prune_tokens an.
    "hermes-qwen-ctx131072": ("bin/hermes-task",
                              {"HERMES_HOME": os.path.expanduser("~/.hermes-arena/ctx131072")},
                              "hermes", "Qwen3.8-27B-Instruct", "local"),
    # Dritte Variante, nach dem Befund aus dem prune28k-Lauf: Pruning allein reicht nicht,
    # weil dort in vier von sechs Runden das Reasoning das Ausgabebudget aufbrauchte, bevor
    # eine sichtbare Antwort entstand. reasoning_effort: low ist der Hebel, den Hermes'
    # eigener Fehlertext zuerst nennt; max_tokens: 16384 ist der zweite (war ungesetzt).
    "hermes-qwen-prune-low": ("bin/hermes-task",
                              {"HERMES_HOME": os.path.expanduser("~/.hermes-arena/prune28k-low")},
                              "hermes", "Qwen3.8-27B-Instruct", "local"),
    "hermes-qwen-prune28k":  ("bin/hermes-task",
                              {"HERMES_HOME": os.path.expanduser("~/.hermes-arena/prune28k")},
                              "hermes", "Qwen3.8-27B-Instruct", "local"),
    # Same harness as hermes-qwen, different model: isolates the MODEL effect on the row
    # whose context handling gave out in run G. Uses bin/hermes-task-model so that the
    # pinned hermes-qwen wrapper -- the one run G's numbers refer to -- stays untouched.
    "hermes-claude-sonnet-5": ("bin/hermes-task-model",
                               {"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-sonnet-5"},
                               "hermes", "claude-sonnet-5", "remote"),
    "hermes-claude-opus-5":   ("bin/hermes-task-model",
                               {"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-opus-5"},
                               "hermes", "claude-opus-5", "remote"),
    "hermes-claude-fable-5-1": ("bin/hermes-task-model",
                               {"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": "claude-fable-5-1"},
                               "hermes", "claude-fable-5-1", "remote"),
    # Claude Code driving the same models as the hermes-claude-* rows: with the model held
    # constant, the difference between these rows is the HARNESS, which is the comparison
    # this arena exists for.
    "cc-opus-5":     ("bin/claude-code-task", {"CC_MODEL": "claude-opus-5"},
                      "claude-code", "claude-opus-5", "remote"),
    "cc-sonnet-5":   ("bin/claude-code-task", {"CC_MODEL": "claude-sonnet-5"},
                      "claude-code", "claude-sonnet-5", "remote"),
    "cc-fable-5-1":  ("bin/claude-code-task", {"CC_MODEL": "claude-fable-5-1"},
                      "claude-code", "claude-fable-5-1", "remote"),
    "hermes-claude-haiku-4-5": ("bin/hermes-task-model",
                               {"HERMES_PROVIDER": "anthropic",
                                "HERMES_MODEL": "claude-haiku-4-5-20251001"},
                               "hermes", "claude-haiku-4-5-20251001", "remote"),
}

# --- v3 rows. Jede Zeile hat ihren eigenen Proxy (PROXY unten); die Umgebung zeigt darauf. ---
HA = os.path.expanduser("~/.hermes-arena")
for _m, _p in (("opus-5", 8020), ("fable-5-1", 8021), ("sonnet-5", 8022), ("haiku-4-5", 8023)):
    _model = "claude-haiku-4-5-20251001" if _m == "haiku-4-5" else f"claude-{_m}"
    WRAPPERS[f"v3-hermes-claude-{_m}"] = ("bin/hermes-task-model",
        {"HERMES_PROVIDER": "anthropic", "HERMES_MODEL": _model, "HERMES_HOME": f"{HA}/v3-claude-{_m}"},
        "hermes", _model, "remote")
    WRAPPERS[f"v3-cc-{_m}"] = ("bin/claude-code-task",
        {"CC_MODEL": _model, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{_p + 4}"},
        "claude-code", _model, "remote")
for _m, _p in (("sol", 8029), ("terra", 8030), ("luna", 8031)):
    _model = f"gpt-5.6-{_m}"
    WRAPPERS[f"v3-hermes-{_model}"] = ("bin/hermes-task-model",
        {"HERMES_PROVIDER": "openai-codex", "HERMES_MODEL": _model,
         # Codex's OAuth resolver deliberately ignores model.base_url. This env override
         # preserves the OAuth bearer while routing Responses requests through the per-row
         # trace proxy, so request/token telemetry is captured rather than silently bypassed.
         "HERMES_CODEX_BASE_URL": f"http://127.0.0.1:{_p}",
         "HERMES_HOME": os.path.expanduser(f"~/.hermes/profiles/arena-gpt56-{_m}")},
        "hermes", _model, "remote")
# rec2/rec2-low: the corrected bundle after rec's round-1 timeout (protect_last_n/
# target_ratio pinned every compaction back to the threshold -- see bin/hermes-variant).
# Ports 8014/8015 are new, not a reuse of rec/rec-low's 8010/8012: the old rows stay
# addressable (and their HERMES_HOME/config.yaml untouched) even though the row is closed.
# rec3 (port 8016): rec2-low + a custom_providers extra_body that silences thinking in
# Hermes's compaction call only (see bin/hermes-variant for the vLLM merge_kwargs rationale).
# rec4 (port 8017): rec2 (reasoning_effort stays medium) + proactive pruning of large tool
# results + a custom_providers extra_body that caps max_tokens -- both measured problems on
# live runs (see bin/hermes-variant for the tool-result-share numbers and the finding that
# model.max_tokens was never wired into the CLI's own agent construction).
# rec3-131k (port 8018): rec3 + the serving-axis switch. context_length 131072 (Hermes
# treats this as an override, not a discovery: leaving it at rec3's 65536 while the server
# serves 131072 is the v2 trap) and threshold_tokens 90000 (scaled with the window; see
# bin/hermes-variant). Briefly renamed rec3-98k on 2026-09-11 when a flat
# kv_cache_max_concurrency >= 2.0 gate read 131072/209132 = 1.60 as a FAIL; reverted the same
# day once the box's own measurements showed the pool grows with the window (it is not a
# constant 2.0x-of-window quantity) and our proxy traces showed Hermes is strictly
# sequential (inflight == 1 in 2247/2248 calls), so the real requirement is 1.25 windows of
# pool, not 2.0 -- see bin/arena-preflight-v3 and bin/hermes-variant for the full derivation.
# Port 8018 kept throughout.
for _v, _p in (("rec", 8010), ("stock", 8011), ("rec-low", 8012), ("rec-131k", 8013),
               ("rec2", 8014), ("rec2-low", 8015), ("rec3", 8016), ("rec4", 8017),
               ("rec3-131k", 8018), ("rec3-131k-prune", 8019)):
    WRAPPERS[f"v3-hermes-qwen-{_v}"] = ("bin/hermes-task", {"HERMES_HOME": f"{HA}/v3-{_v}"},
                                        "hermes", "Qwen3.8-27B-Instruct", "local")
# Prompt hygiene is a separate experimental row so it cannot contaminate the
# pruning-only comparison.
WRAPPERS["v3-hermes-qwen-rec3-131k-prune-hygiene"] = (
    "bin/hermes-task",
    {"HERMES_HOME": f"{HA}/v3-rec3-131k-prune-hygiene", "ARENA_AGENT_OUTPUT_HYGIENE": "1"},
    "hermes", "Qwen3.8-27B-Instruct", "local")

# row -> (dialect, upstream, port). Ein Proxy-Prozess je Zeile, vom Runner gestartet.
ANTHROPIC = "https://api.anthropic.com"
PROXY = {}
for _m, _p in (("opus-5", 8020), ("fable-5-1", 8021), ("sonnet-5", 8022), ("haiku-4-5", 8023)):
    PROXY[f"v3-hermes-claude-{_m}"] = ("anthropic", ANTHROPIC, _p)
    PROXY[f"v3-cc-{_m}"] = ("anthropic", ANTHROPIC, _p + 4)
for _m, _p in (("sol", 8029), ("terra", 8030), ("luna", 8031)):
    PROXY[f"v3-hermes-gpt-5.6-{_m}"] = ("codex", "https://chatgpt.com/backend-api/codex", _p)
for _v, _p in (("rec", 8010), ("stock", 8011), ("rec-low", 8012), ("rec-131k", 8013),
               ("rec2", 8014), ("rec2-low", 8015), ("rec3", 8016), ("rec4", 8017),
               ("rec3-131k", 8018), ("rec3-131k-prune", 8019)):
    PROXY[f"v3-hermes-qwen-{_v}"] = ("openai", "http://127.0.0.1:8000", _p)
PROXY["v3-hermes-qwen-rec3-131k-prune-hygiene"] = ("openai", "http://127.0.0.1:8000", 8028)


def backend_of(row):
    return WRAPPERS[row][4]
EXIT_MEANING = {0: "ok", 3: "timeout/bad-dir", 4: "endpoint-unreachable",
                5: "no-output", 7: "missing-credential", 8: "harness-failure",
                9: "session-not-resumable"}


def median_grade(grades):
    """Median je Check (Mehrheit), Median des Gates, Spanne der Capability; rejected vereinigt.

    Ein gescheiterter Grader-Aufruf (capability.checks == {}, total 0) darf die erfolgreichen
    Aufrufe nicht verduennen: an Position 0 hat er frueher Schluessel/total auf 0/{} gezogen und
    jeden Check-Wert und jeden Capability-Wert der anderen zwei mit-ueberschrieben ([fail, ok,
    ok] -> passed 0/total 0). Schluessel/total kommen jetzt vom ersten ERFOLGREICHEN Eintrag,
    und Mehrheit/Median/Spanne laufen nur ueber die erfolgreichen Eintraege. Screenshots und
    rejected bleiben ueber ALLE Eintraege vereinigt -- ein gescheiterter Grader kann trotzdem
    Screenshots oder eine rejected-Note hinterlassen haben.
    """
    import statistics
    ok = [g for g in grades if g.get("capability", {}).get("checks")]
    rejected = sorted({r for g in grades for r in g.get("rejected", [])})
    screenshots = [s for g in grades for s in g.get("screenshots", [])]
    if not ok:
        return {"gate": 0, "rung": 0, "capability": {"passed": 0, "total": 0, "checks": {}},
                "span": [0, 0], "rejected": rejected, "screenshots": screenshots, "fps": 0,
                "grades": grades, "grades_failed": len(grades)}
    checks = {}
    for k in ok[0]["capability"]["checks"]:
        votes = [bool(g["capability"]["checks"].get(k)) for g in ok]
        checks[k] = sum(votes) * 2 > len(votes)
    caps = sorted(g["capability"]["passed"] for g in ok)
    return {"gate": int(statistics.median(g["gate"] for g in ok)),
            "capability": {"passed": sum(checks.values()), "total": ok[0]["capability"]["total"], "checks": checks},
            "span": [caps[0], caps[-1]], "rejected": rejected, "screenshots": screenshots,
            "fps": statistics.median(g.get("fps", 0) or 0 for g in ok), "grades": grades,
            "grades_failed": len(grades) - len(ok)}


def kill_grader_group(pgid, proc=None, grace_s=10):
    """Erst SIGINT an die GRUPPE des Graders (grader.py faehrt sein finally, d.quit() nimmt
    geckodriver und Firefox mit), nach grace_s SIGKILL. Die Gruppe ist die eigene Sitzung des
    Graders (start_new_session), nie die des Runners -- die teilt der Runner mit arena-contest
    und allen Zeilen."""
    try:
        os.killpg(pgid, signal.SIGINT)
    except OSError:
        return "gone"
    if proc is not None:
        try:
            proc.wait(grace_s)
            return "interrupted"
        except subprocess.TimeoutExpired:
            pass
    else:
        time.sleep(min(grace_s, 2))
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    if proc is not None:
        try: proc.wait(5)
        except subprocess.TimeoutExpired: pass
    return "killed"


def reap_grader_group(pgid):
    """Was nach dem Grader noch in SEINER Gruppe lebt, hart beenden.

    Zweimal am 2026-09-10 ueberlebte ein headless Firefox seinen Grader und lief mit 98 % CPU
    weiter -- gegen die fps-Messung jeder folgenden Bewertung, also gegen genau die Zahl, fuer
    die es den maschinenweiten Lock gibt. Ein Prozess, der den Grader ueberlebt, hat kein
    Ergebnis mehr, an dem jemand haengt; er ist nur noch Last. Sicher ist das, weil die Gruppe
    dem Grader allein gehoert (start_new_session=True beim Popen)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True                      # es lebte noch etwas
    except OSError:
        return False                     # ESRCH: die Gruppe ist leer, der Normalfall


def grader_bin(bench):
    """Welcher Grader laeuft: die Bank-eigene grader.py, sonst das gemeinsame ladder_check.py.

    ARENA_GRADER_BIN setzt beides ausser Kraft. Das ist der einzige Weg, die Fliessband-Logik
    (welcher Schnappschuss wann bewertet wird, in welcher Reihenfolge die Ergebnisse landen)
    ohne Browser und ohne Netz zu pruefen: der Stand-in schlaeft und druckt eine feste
    JSON-Zeile. Ein Lauf, der ihn benutzt, ist keine Messung -- er steht in keiner Provenance.
    """
    override = os.environ.get("ARENA_GRADER_BIN")
    if override:
        return override
    local = os.path.join(bench, "grader.py")
    return local if os.path.exists(local) else os.path.join(HERE, "ladder_check.py")


def grade(workdir, bench, run_dir=None, rnd=0, cfg=None, on_start=None):
    """Score the build by actually driving it in a browser.

    The text-based check this replaced looked for '<canvas' and 'requestAnimationFrame' in
    the source and would happily award a rung to a stub that draws nothing. Rungs are only
    awarded on observed behaviour.

    A benchmark may ship its own grader as benchmarks/<game>/grader.py; otherwise the shared
    ladder_check.py runs. Both print one JSON object; the whole object is carried into the
    result, because v2's grader returns a capability checklist next to the gate rung and
    dropping it here would throw away the part that grows across rounds.

    v2: ein Aufruf, Ergebnis wie gehabt. v3 (grade_repeats > 1): n Aufrufe mit eigenen Seeds
    und Screenshot-Verzeichnissen, Median. Unter flock, weil fps im Browser DIESER Maschine
    gemessen wird.
    """
    cfg = cfg or load_bench_cfg(bench)
    if not os.path.exists(os.path.join(workdir, "index.html")):
        return {"rung": 0, "gate": 0, "notes": ["no index.html"]}
    grader = grader_bin(bench)
    # Grading is serialised across every concurrent row. The checklist contains ">=55 fps",
    # measured by a requestAnimationFrame counter in a headless Firefox on THIS machine, so
    # two graders running at once measure each other's CPU contention and both report a
    # build as slower than it is. Rows may run their agents in parallel; they may not grade
    # in parallel. The lock is held only for the grading window, not for the round.
    # ARENA_GRADER_LOCK zeigt woanders hin, wenn nicht wirklich ein Browser gemessen wird
    # (Stand-in-Grader in den Tests). Sonst stellt sich ein Testlauf in die Schlange EINER
    # laufenden Kampagne -- er wartet minutenlang und haelt danach seinerseits neun echte
    # Zeilen auf, fuer eine Bewertung, die nur schlaeft.
    lock_path = os.environ.get("ARENA_GRADER_LOCK") or os.path.join(tempfile.gettempdir(),
                                                                    "arena-grader.lock")
    # Deckel je EINZELNER Bewertung: Aufbau (Titel, Start, Eingabe) ~60 s + Fenster <= 300 s +
    # Screenshots + Abbau, mit Reserve. 0 schaltet ihn ab.
    grade_timeout = int(os.environ.get("ARENA_GRADE_TIMEOUT_S", "900"))
    results = []
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for k in range(cfg["grade_repeats"]):
            argv = [sys.executable, grader, workdir, "--json"]
            if cfg["grade_repeats"] > 1:
                shots = os.path.join(run_dir, f"r{rnd}", f"grade-{k}")
                argv += ["--seed", f"{RUN_ID}-{rnd}-{k}", "--shots", shots]
            # start_new_session: der Grader bekommt eine EIGENE Prozessgruppe, mitsamt
            # geckodriver und Firefox. Ohne das teilt er die Gruppe des Runners -- und die
            # teilt der Runner mit arena-contest und allen acht Zeilen, ein killpg darauf
            # wuerde die ganze Kampagne umlegen. Der Aufrufer (Grading) haelt den Griff, damit
            # main()s finally das Kind beim Stopp erreicht: der Bewertungsthread ist ein
            # daemon, er sieht das KeyboardInterrupt nie, und ein verwaister Grader misst
            # danach gegen die naechste Bewertung an.
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            if on_start:
                on_start(proc)
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = proc.pid          # start_new_session: die Gruppe IST die pid
            timed_out = False
            try:
                out, err = proc.communicate(timeout=grade_timeout or None)
            except subprocess.TimeoutExpired:
                # Gemessen am 2026-09-10 (cc-haiku r2): eine Bewertung brauchte ~26 min bei
                # einem Fenster von 200-290 s -- der Grader stand ~20 min in einem
                # WebDriver-Aufruf, bevor die Sitzung ueberhaupt begann. Woran das im Browser
                # liegt, ist hier egal: eine Bewertung, die laenger dauert als ihr Fenster
                # plus Aufbau, ist kein Messwert mehr, und sie blockiert ueber den Lock jede
                # andere Zeile. Der Runner deckelt sie.
                timed_out = True
                how = kill_grader_group(pgid, proc)
                # Nachraeumen VOR dem Einsammeln der Ausgabe. Ein Ueberlebender der Gruppe
                # (genau der Firefox, um den es geht) haelt das stdout-Rohr des Graders offen,
                # und communicate() wartet auf dessen EOF -- der Deckel haette sich sonst
                # selbst aufgehoben und der Runner haengt dann STATT des Graders. Beim ersten
                # Testlauf ist genau das passiert.
                left = reap_grader_group(pgid)
                try:
                    out, err = proc.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill(); out, err = "", ""
                print(f"round {rnd} grading {k + 1}/{cfg['grade_repeats']}: "
                      f"timeout after {grade_timeout} s, {how}"
                      f"{' (a process of its group outlived it)' if left else ''}", flush=True)
            else:
                # Auch nach einem NORMALEN Ende: was in der Gruppe des Graders weiterlebt, hat
                # kein Ergebnis mehr, an dem jemand haengt -- nur CPU gegen die naechste fps.
                if reap_grader_group(pgid):
                    print(f"round {rnd} grading {k + 1}/{cfg['grade_repeats']}: "
                          f"killed a process that outlived the grader", flush=True)
            if timed_out:
                # Eintragsform wie ein gescheiterter Grader: leere checks, damit median_grade
                # ihn aussortiert statt den Median zu verduennen, und grades_failed ihn zaehlt.
                results.append({"rung": 0, "gate": 0, "capability": {"passed": 0, "total": 0, "checks": {}},
                                "notes": [f"grader timeout after {grade_timeout}s"]})
                continue
            try:
                results.append(json.loads(out.strip().splitlines()[-1]))
            except Exception:
                results.append({"rung": 0, "gate": 0, "capability": {"passed": 0, "total": 0, "checks": {}},
                                "notes": [f"grader failed: {(err or out)[-200:]}"]})
    if cfg["grade_repeats"] == 1:
        return results[0]
    m = median_grade(results); m["rung"] = m["gate"]
    # "grader failed" (the subprocess produced no parsable JSON) and "harness error" (the
    # grader ran but could not drive a browser) were filtered OUT of ladder_note, so a round
    # whose grading collapsed read in the results file exactly like a round that was graded
    # and scored low. They are the two notes that explain a zero; they belong in the note.
    m["notes"] = [n for g in results for n in g.get("notes", [])
                  if n.startswith(("gate stops", "rejected", "keys", "touch", "title", "start",
                                   "grader failed", "grader timeout", "harness error"))]
    return m


def harness_config(row):
    """Die Einstellungen, gegen die diese Zeile tatsaechlich faehrt.

    Ohne das ist aus den Artefakten nicht zu erkennen, welches Setup eine Zahl erzeugt hat:
    zwei Zeilen koennen denselben Wrapper, dasselbe Modell und denselben Endpunkt haben und
    sich nur in der Kontextfuehrung der Harness unterscheiden -- genau der A/B-Test, um den
    es hier geht. Gelesen wird die Wurzel, die die Zeile per HERMES_HOME benutzt.
    """
    extra = WRAPPERS[row][1] or {}
    out = {k: v for k, v in extra.items() if k in ("HERMES_HOME", "HERMES_PROVIDER",
                                                   "HERMES_MODEL", "CC_MODEL")}
    home = extra.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    cfg = os.path.join(home, "config.yaml")
    if WRAPPERS[row][2] == "hermes" and os.path.exists(cfg):
        import re
        text = open(cfg, encoding="utf-8", errors="replace").read()
        for key in ("context_length", "max_tokens", "threshold_tokens", "proactive_prune_tokens",
                    "max_attempts", "min_tail_user_messages", "threshold", "tail_mode",
                    "reasoning_effort", "base_url", "provider"):
            m = re.search(rf"^\s*{key}:\s*(\S+)", text, re.M)
            if m:
                out[key] = m.group(1)
    return out


def _sha(path):
    import hashlib
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16] if os.path.exists(path) else None


def _sha_many(d, names):
    import hashlib
    h = hashlib.sha256()
    for n in names:
        p = os.path.join(d, n)
        if os.path.exists(p): h.update(open(p, "rb").read())
    return h.hexdigest()[:16]


# Referenz je Zeilen-Praefix fuer hermes_diff. Normalerweise v3-rec (Spec 4.2s Basislinie).
# rec2 ist ein KORRIGIERTES BUENDEL, kein Ein-Knopf-Unterschied gegen rec (drei Knoepfe auf
# einmal, siehe bin/hermes-variant) -- gegen v3-rec verglichen wuerde der Diff fuer rec2-low
# staendig rec2s drei Buendel-Zeilen zusammen mit der einen reasoning_effort-Zeile zeigen und
# den tatsaechlichen Ein-Knopf-Unterschied von rec2-low verdecken. rec2-Zeilen vergleichen
# sich deshalb gegen v3-rec2. Praefix-Liste statt einzelner Zeilennamen, damit ein kuenftiges
# rec2-* (z.B. ein rec2-131k) die richtige Referenz automatisch bekommt. rec3 ist wiederum
# rec2-low plus GENAU ein Unterschied (die custom_providers-Ergaenzung) -- seine Referenz ist
# deshalb v3-rec2-low, nicht v3-rec2 und nicht v3-rec, aus demselben Grund. rec4 ist rec2
# (nicht rec2-low: reasoning_effort bleibt medium) plus zwei Ergaenzungen (proaktives Pruning,
# custom_providers max_tokens) -- Referenz v3-rec2, exakt aufgelistet statt per Praefix, weil
# "v3-hermes-qwen-rec4" nicht mit "v3-hermes-qwen-rec2" beginnt. rec3-131k ist rec3 plus
# GENAU zwei Unterschiede (context_length, threshold_tokens) -- Referenz v3-rec3. Der Eintrag
# MUSS vor dem allgemeineren "v3-hermes-qwen-rec3"-Praefix stehen: "v3-hermes-qwen-rec3-131k"
# beginnt selbst mit "v3-hermes-qwen-rec3", und die Liste liefert die ERSTE passende Referenz.
HERMES_DIFF_REFERENCE = [("v3-hermes-qwen-rec2", "v3-rec2"),
                          ("v3-hermes-qwen-rec3-131k-prune", "v3-rec3-131k"),
                          ("v3-hermes-qwen-rec3-131k", "v3-rec3"),
                          ("v3-hermes-qwen-rec3", "v3-rec2-low"),
                          ("v3-hermes-qwen-rec4", "v3-rec2")]


def _hermes_diff_reference(row):
    for prefix, ref in HERMES_DIFF_REFERENCE:
        if row.startswith(prefix):
            return ref
    return "v3-rec"


def hermes_diff(row):
    """Unterschied der Varianten-config.yaml zur Referenz (siehe _hermes_diff_reference), zeilenweise.

    `_config_version` ist Hermes' eigene Schema-Migrationsnummer, keine Zeilen-Einstellung --
    sie bewegt sich mit der Arbeitsinstallation, unabhaengig davon, wann eine Variante zuletzt
    neu erzeugt wurde (beobachtet: v3-rec3-131k (damals kurz v3-rec3-98k genannt) zeigte
    41->42 gegen die damalige v3-rec3, obwohl beide Configs sonst identisch waren -- v3-rec3
    wurde daraufhin neu erzeugt, siehe bin/hermes-variant-Historie). Aus dem Diff
    ausgeschlossen, sonst zeigt jede Variante, die nach einer Hermes-Migration neu erzeugt
    wird, einen Unterschied, der keiner ist.
    """
    home = (WRAPPERS[row][1] or {}).get("HERMES_HOME")
    ref_name = _hermes_diff_reference(row)
    ref = os.path.join(HA, ref_name, "config.yaml")
    if not home or not os.path.exists(ref) or not os.path.exists(os.path.join(home, "config.yaml")): return None
    import difflib
    a = open(ref).read().splitlines(); b = open(os.path.join(home, "config.yaml")).read().splitlines()
    return [l for l in difflib.unified_diff(a, b, ref_name, os.path.basename(home), lineterm="", n=0)
            if l[:1] in "+-" and l[:3] not in ("+++", "---") and not l[1:].startswith("_config_version:")]


def provenance(row, game, run_id):
    """Record what actually ran, at the moment it ran.

    Hand-written provenance drifts: the previous run log recorded the harness commit of a
    reference checkout rather than of the installed binary, and nobody could tell from the
    artefacts. Everything here is read from the system at launch.
    """
    def sh(*cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else ""
        except Exception as exc:
            return f"unavailable: {type(exc).__name__}"

    def sh_all(*cmd):
        """Like sh(), but the whole output -- for things that are counted, not quoted."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    # Only a LOCAL row talks to the vLLM endpoint. Recording its model id and window for a
    # remote row is not merely useless, it is false: contest H's Claude rows each recorded
    # `model_reported_by_endpoint: Qwen3.8-27B-Instruct` while running claude-opus-5, and a
    # reader of those files would conclude the wrong model produced the numbers.
    backend_now = WRAPPERS[row][4]
    model_id = "unavailable"
    max_len = None
    base = os.environ.get("QWEN_BASE_URL", "").rstrip("/")
    if backend_now != "local":
        model_id = f"not applicable: {backend_now} row, never calls this endpoint"
        base = ""
    elif base:
        try:
            import urllib.request
            raw = urllib.request.urlopen(base + "/models", timeout=20).read().decode()
            entry = (json.loads(raw).get("data") or [{}])[0]
            model_id = entry.get("id", "")
            # The served context window, READ from the server rather than assumed. It is the
            # one serving property this benchmark's outcome hinges on -- run G's round 2 died
            # at 65,536 -- and it was doubled to 131,072 on 2026-09-09 with nothing in the
            # record noticing. `serving_config` is a label nobody verifies; this is a
            # measurement, and it is what makes two runs comparable or not.
            max_len = entry.get("max_model_len")
        except Exception as exc:
            model_id = f"unavailable: {type(exc).__name__}"
            max_len = None

    wrapper, _, harness, model, backend = WRAPPERS[row]
    return {
        "run_id": run_id, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "row": row, "game": game, "harness": harness, "wrapper": wrapper,
        "model_declared": model, "model_reported_by_endpoint": model_id,
        "max_model_len_reported": max_len,
        "harness_config": harness_config(row),
        # Der Stack, nicht nur der Modellname (siehe vllm_metrics.stack_fingerprint).
        "serving_stack": (vllm_metrics.stack_fingerprint()
                          if backend_now == "local" else
                          {"note": f"{backend_now} row -- kein lokaler Stack"}),
        "endpoint": base,
        "backend": backend,
        "serving_config_label": os.environ.get("ARENA_SERVING_CONFIG", "setup-1"),
        "serving_config_note": "a label passed in, NOT verified against the server",
        "agent_host": os.uname().nodename,
        "arena_commit": sh("git", "-C", HERE, "rev-parse", "--short", "HEAD"),
        # sh() deliberately returns only the first line, which made this counter report 1
        # for any dirty tree -- run G's provenance says "1" with 15 paths modified. Count
        # the real output instead.
        "arena_dirty_paths": len(sh_all("git", "-C", HERE, "status", "--porcelain").splitlines()),
        "hermes_version": sh(os.path.expanduser("~/.local/bin/hermes"), "--version"),
        "firefox": sh("/snap/bin/firefox", "--version"),
        "geckodriver": sh("/snap/bin/geckodriver", "--version"),
        "python": sys.version.split()[0],
        "agent_sandbox": agent_sandbox.status(),
        "bench_cfg": load_bench_cfg(os.path.join(HERE, "benchmarks", game)),
        "grader_sha256": _sha(os.path.join(HERE, "benchmarks", game, "grader.py")),
        "bench_sha256": _sha_many(os.path.join(HERE, "benchmarks", game), ("base.txt", "rounds.json", "bench.json")),
        "proxy_sha256": _sha(os.path.join(HERE, "tools", "trace_proxy.py")),
        "proxy": ({"dialect": PROXY[row][0], "upstream": PROXY[row][1], "port": PROXY[row][2]} if row in PROXY else None),
        "hermes_config_diff": hermes_diff(row),
    }


def compose(bench, rnd, workdir, continuing=False):
    """Runde 1 (oder Fallback): Spec + Auftrag. Fortgesetzte Sitzung: nur der neue Auftrag --
    der Agent hat Spec und eigene Arbeit im Kontext, so wie ein Mensch nachlegt (Spec 2.4/2.5).

    The first run of this benchmark re-sent round 1's task every round, so rounds 2 and 3
    were told to build a scope that was already finished and correctly did nothing. The
    per-round instruction is the whole point of a multi-round build.
    """
    cfg = load_bench_cfg(bench)
    base = open(os.path.join(bench, "base.txt"), encoding="utf-8").read()
    rounds = json.load(open(os.path.join(bench, "rounds.json"), encoding="utf-8"))
    if continuing and cfg["session_mode"] == "continue":
        task = (f"--- ROUND {rnd} of {len(rounds)} (follow-up in this same session) ---\n{rounds[rnd-1]}\n\n"
                "Your files are in the current working directory. Keep every earlier round working.\n")
        return task, len(rounds)
    task = f"{base}\n\n--- YOUR TASK THIS ROUND ({rnd} of {len(rounds)}) ---\n{rounds[rnd-1]}\n"
    prior = os.path.join(workdir, "index.html")
    if rnd > 1 and os.path.exists(prior):
        if cfg["paste_prior_file"]:
            cur = open(prior, encoding="utf-8", errors="replace").read()
            task += (f"\nThe file you wrote so far is below ({len(cur)} bytes). Extend it and "
                     f"WRITE the updated index.html back to disk.\n\n--- CURRENT index.html ---\n{cur}\n--- END ---\n")
        else:
            task += ("\nYour files from the previous rounds are in the current working directory. "
                     "Read what you need, change what this round asks for, and keep everything else working.\n")
    return task, len(rounds)


def session_for(run_id, fallback_round=None):
    """Deterministische Sitzungskennungen je Lauf: Hermes bekommt einen Namen, Claude Code eine UUID.
    Nach einem Fallback (Spec 2.5) ein neuer Name/UUID, damit die alte Sitzung nicht wieder geladen wird."""
    import uuid
    name = f"arena-{run_id}" + (f"-fresh-r{fallback_round}" if fallback_round else "")
    return {"name": name, "uuid": str(uuid.uuid5(uuid.NAMESPACE_URL, "arena/" + name))}


def session_env(cfg, session, continuing):
    """Sitzungsvariablen gehen NUR bei session_mode == "continue" hinaus: eine v2-Zeile (kein
    bench.json, session_mode "fresh") darf keine der drei Variablen je sehen, sonst verhaelt
    sich ihr Wrapper nicht mehr byteidentisch zu vor v3."""
    if cfg.get("session_mode") != "continue":
        return {}
    return {"ARENA_SESSION_NAME": session["name"], "ARENA_SESSION_ID": session["uuid"],
            "ARENA_SESSION_CONTINUE": "1" if continuing else "0"}


def urllib_post(url, obj):
    import urllib.request
    return urllib.request.urlopen(urllib.request.Request(
        url, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"}, method="POST"),
        timeout=10).read()


def latest_mtime(workdir):
    m = 0.0
    for dp, dn, fn in os.walk(workdir):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in fn:
            if f.startswith("."): continue   # wie build_hash(): ein Dotfile ist kein Lebenszeichen
            try: m = max(m, os.stat(os.path.join(dp, f)).st_mtime)
            except OSError: pass
    return m


def proxy_last_call(port):
    try:
        import urllib.request
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/__arena/stats", timeout=5).read()).get("last_call_t") or 0.0
    except Exception:
        return 0.0


def proxy_inflight_now(port):
    """Laeuft GERADE ein Aufruf (streamt noch, noch kein last_call_t)? Ein einzelner langer
    Stream (grosser Kontext, langsames Modell) darf nicht als Stillstand gelesen werden, nur
    weil er noch keinen ABGESCHLOSSENEN Aufruf hinterlassen hat."""
    try:
        import urllib.request
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/__arena/stats", timeout=5).read()).get("inflight_now", 0)
    except Exception:
        return 0


def abort_reason(res):
    """Ist diese Runde nach Aarons Regel ein Abbruch? Gibt den Grund zurueck oder None.

    Zwei Faelle, beide in der Kampagne beobachtet: der Harness bricht selbst ab (exit 8,
    typisch "max compression attempts (3) reached") oder der Runner beendet den Agenten
    nach Zeit- oder Stillstandsgrenze. Eine Runde mit exit 0 ist KEIN Abbruch, auch wenn
    sie nichts geaendert hat -- ein No-op ist ein Ergebnis, kein Ausfall. exit 4 (Endpunkt
    tot) ist ebenfalls keiner: das ist ein Infrastrukturausfall, der die Zeile nicht
    bewertet, und er hat seinen eigenen Abbruchpfad weiter oben.
    """
    if res.get("exit_code") == 8:
        return "harness failure"
    if res.get("ended_by") in ("timeout", "stalled"):
        return res["ended_by"]
    return None


def stall_s_for(bench):
    """Stall-Erkennung nur fuer Baenke MIT bench.json (v3): eine v2-Zeile (kein bench.json,
    session_mode "fresh") bekommt stall_s=0 (aus) -- ihre Rundenlaenge war nie unter dem
    Stall-Fenster vermessen, und ein falscher Abbruch waere teurer als ein haengender Lauf, der
    von Hand beendet wird. ARENA_STALL_S in der Umgebung gewinnt in jedem Fall."""
    default = "900" if os.path.exists(os.path.join(bench, "bench.json")) else "0"
    return int(os.environ.get("ARENA_STALL_S", default))


def wait_round(proc, task, timeout_s, workdir, proxy_port, stall_s, poll_s=30):
    """Wartet auf den Agenten; beendet ihn bei Timeout ODER Stillstand. Stillstand = stall_s lang
    weder ein beendeter Modellaufruf (Proxy) noch ein laufender Stream (inflight_now > 0) noch
    eine geaenderte Datei noch stdout; stall_s == 0 heisst NIE stallen. Vorher wartete der Runner
    bis --timeout (3 h) auf eine Runde, die nach 20 Minuten tot war.

    Stdin wird auf einem EIGENEN Thread geschrieben, nicht auf dem Hauptthread: ein Wrapper, der
    beendet OHNE stdin zu lesen (hermes-task-model exit 4 bei abgemeldetem Provider,
    claude-code-task exit 3/4 bei falschem Verzeichnis/Binary, Task 5b exit 9 bei verlorener
    Sitzung), liess `proc.stdin.write(task)` mit BrokenPipeError platzen -- gefangen vom
    generischen except in run_round, als rc 5 "runner error" verbucht, und das setzte sowohl den
    exit-9-Fallback als auch den exit-4-Stopp ausser Kraft, weil run_round nie den echten
    Exit-Code sah. Und ein v2-Prompt mit angehaengter Datei sprengt leicht den 64-KB-Pipe-Puffer:
    ungeschuetzt auf dem Hauptthread wuerde der Schreibvorgang blockieren, bis der Agent liest --
    ausserhalb der Stall-/Timeout-Schleife. (rc, out, ended_by)."""
    import threading
    lines = []
    def pump():
        for line in proc.stdout:
            lines.append(line)
    def feed():
        try:
            proc.stdin.write(task); proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass   # der Wrapper hat sich schon beendet, ohne stdin zu lesen -- kein Rundenfehler
    th = threading.Thread(target=pump, daemon=True); th.start()
    wr = threading.Thread(target=feed, daemon=True); wr.start()
    t0 = time.time(); seen = 0; alive = t0; ended_by = None
    # Spec 6: der Ereignisstreifen zeigt "erstes Schreiben einer Datei" als Marker. Die Uhr
    # dafuer laeuft hier -- der erste Poll, in dem eine Datei im Arbeitsverzeichnis juenger
    # ist als der Rundenstart. Alle poll_s Sekunden abgetastet, also auf poll_s genau; das
    # steht im Tooltip der Seite, damit niemand die Zahl feiner liest, als sie ist.
    mtime0 = latest_mtime(workdir); first_write_t = None
    while True:
        try:
            proc.wait(timeout=poll_s)
            break                              # der Agent ist von selbst fertig -- sofort zurueck
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if len(lines) > seen: seen = len(lines); alive = now
        mt = latest_mtime(workdir)
        if first_write_t is None and mt > mtime0:
            first_write_t = now
        alive = max(alive, mt)
        if proxy_port:
            alive = max(alive, proxy_last_call(proxy_port))
            if proxy_inflight_now(proxy_port) > 0:
                alive = now                    # ein laufender Stream ist selbst ein Lebenszeichen
        if timeout_s and now - t0 > timeout_s: ended_by = "timeout"; break
        if stall_s and now - alive > stall_s: ended_by = "stalled"; break
    if ended_by:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # die Gruppe, nicht nur die bash
        proc.wait()
    th.join(5); wr.join(5)
    # Eine Runde, die vor dem ersten Poll fertig ist, hat trotzdem geschrieben -- dann steht
    # der Marker beim Ende der Runde, nicht gar nicht.
    if first_write_t is None and latest_mtime(workdir) > mtime0:
        first_write_t = time.time()
    return (3 if ended_by else proc.returncode), "".join(lines), ended_by, first_write_t


def run_round(row, game, rnd, workdir, bench, timeout_s, run_dir, cfg, proxy_port, session, continuing):
    wrapper, extra_env, harness, model, backend = WRAPPERS[row]
    wrapper = wrapper if os.path.isabs(wrapper) else os.path.join(HERE, wrapper)
    env = dict(os.environ); env.update(extra_env)
    env.update(session_env(cfg, session, continuing))

    task, _ = compose(bench, rnd, workdir, continuing)
    if proxy_port:
        try:
            urllib_post(f"http://127.0.0.1:{proxy_port}/__arena/round", {"round": rnd})
        except Exception as exc:
            print(f"round {rnd}: proxy control failed ({exc})", flush=True)

    attrs = dict(config=os.environ.get("ARENA_SERVING_CONFIG", "setup-1"),
                 harness=harness, model=model, game=game, round_no=rnd,
                 run_id=RUN_ID)   # without this, reruns are indistinguishable in Phoenix
    with arena_trace.span(f"round-{rnd}", **attrs) as sp:
        # Model-side counters bracket the round: wall time alone cannot separate a slow
        # model from an agent thinking between calls, and cannot see acceptance or cache.
        # A remote row must not touch :8000 at all: the endpoint is socket-activated, so
        # even a metrics scrape wakes the engine and takes ~23 GB of VRAM from whatever
        # else is on the card -- for a row that does not use the GPU at all.
        mbefore = vllm_metrics.snapshot() if backend == "local" else None
        t0, t0_wall = time.perf_counter(), time.time()
        ended_by = None
        prior_hash = None
        try:
            # cwd=workdir is the load-bearing argument. TERMINAL_CWD is NOT honoured by
            # the agent -- verified by asking it to `cat` a file that existed only in the
            # work directory: it ran the command in this repo instead and reported the file
            # missing. Without cwd set, the agent works in the arena checkout, which is both
            # why nothing ever landed in the work directory and why an earlier round found
            # ladder_check.py and ran the grader instead of building the game.
            # agent_sandbox.argv: the agent runs in a mount namespace with every path
            # holding grader code replaced by an empty tmpfs. cwd alone only stops an
            # agent that walks up from its own directory; nothing stopped one from
            # reading benchmarks/<game>/grader.py by absolute path, and that file names
            # all 24 checks and the __state fields they read.
            prior_hash = build_hash(workdir)   # VOR dem Agentenlauf, direkt vor Popen
            # r<n> wird hier geleert, nicht erst beim Schnappschuss: ein zweiter Anlauf
            # derselben Runde (Exit-9-Fallback, wiederholtes --from-round) soll den alten
            # Build ERSETZEN, und eine Runde, die unterwegs abgebrochen wird, soll ein LEERES
            # Verzeichnis hinterlassen, nie einen veralteten Build. Der Wiedereinstieg
            # unterscheidet beides an r<n>/index.html.
            snap = snapshot_dir(run_dir, rnd)
            if os.path.isdir(snap):
                shutil.rmtree(snap, ignore_errors=True)
            os.makedirs(snap, exist_ok=True)
            proc = subprocess.Popen(agent_sandbox.argv(wrapper, workdir), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, env=env, cwd=workdir,
                                    start_new_session=True)
            rc, out, ended_by, first_write_t = wait_round(proc, task, timeout_s, workdir,
                                                          proxy_port, stall_s_for(bench))
            if ended_by:
                print(f"round {rnd}: {ended_by} after {int(time.time()-t0_wall)}s -- killed, recorded, continuing", flush=True)
        except Exception as exc:
            rc, out, first_write_t = 5, f"runner error: {exc}", None
        wall = time.perf_counter() - t0
        if backend == "local":
            metrics = vllm_metrics.delta(mbefore, vllm_metrics.snapshot(), wall)
        else:
            metrics = {"metrics_note": f"backend={backend}: no local model, no vLLM counters"}
        # arena_ladder_rung und arena_capability_* stehen NICHT mehr an diesem Span: die
        # Bewertung laeuft jetzt hinter dem Agenten her (Grading), der Runden-Span ist da
        # laengst beendet. Sie stehen am Span `grade-<n>`, den der Bewertungsthread oeffnet;
        # arena_round verbindet beide. Die Rundendatei (results_<row>.json) ist unveraendert
        # und bleibt die Quelle, aus der die Auswertung liest.
        for k, v in arena_trace.measurements(
                ctx_tokens_in=len(task) // 4, ctx_tokens_out=len(out) // 4).items():
            sp.set_attribute(k, v)
        sp.set_attribute("arena_exit_code", rc)
        sp.set_attribute("arena_wall_s", round(wall, 2))
        for k, v in metrics.items():
            if v is not None:
                sp.set_attribute(f"arena_{k}", v)

    # Per run, not per workdir: the workdir is wiped by --fresh and was overwriting the
    # stdout of the previous run along with it.
    with open(os.path.join(run_dir, f"stdout-r{rnd}.txt"), "w") as fh:
        fh.write(out or "")
    # Fingerabdruck des Artefakts. Eine Runde, die nichts aendert, erbt sonst die Note der
    # Vorrunde und steht als Erfolg im Protokoll: der Grader bewertet den unveraenderten
    # Build, das Gate bleibt hoch, `exit_meaning` sagt "ok". Gemessen am 2026-09-09:
    # hermes gab exit 0 mit dem Text "No visible answer was produced -- the model hit its
    # output-token limit" zurueck, und die Runde ging als 6/6 durch, ohne dass ein Byte
    # geschrieben wurde. build_hash() statt nur index.html: v3 erlaubt mehrere Dateien.
    build_sha = build_hash(workdir)
    # Der Schnappschuss entsteht VOR der Bewertung, nicht danach: bewertet wird r<n>/, waehrend
    # der Agent der naechsten Runde schon wieder ins Arbeitsverzeichnis schreibt. Frueher lag
    # die Kopie hinter grade() in main(), weil der Grader direkt auf dem Arbeitsverzeichnis
    # lief -- genau die Kopplung, die den Agenten auf die maschinenweite Grader-Schlange
    # warten liess.
    snap = snapshot_dir(run_dir, rnd)   # vor dem Agentenlauf geleert und angelegt
    shutil.copytree(workdir, snap, ignore=shutil.ignore_patterns(".*"), dirs_exist_ok=True)
    proxy_agg = None
    if proxy_port:
        try:
            import urllib.request
            proxy_agg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{proxy_port}/__arena/stats?round={rnd}", timeout=10).read())
            json.dump(proxy_agg, open(os.path.join(snap, "proxy.json"), "w"), indent=1)
        except Exception as exc:
            proxy_agg = {"error": str(exc)[:200]}
    files = sorted(os.path.relpath(os.path.join(dp, f), workdir) for dp, dn, fn in os.walk(workdir)
                   for f in fn if not f.startswith(".") and "/." not in dp.replace(workdir, ""))

    # Der AGENTENTEIL des Rundenergebnisses. Die Bewertungsschluessel stehen schon hier -- auf
    # None -- damit die Reihenfolge der Schluessel in der Datei dieselbe bleibt wie vor dem
    # Fliessband: grading_part() fuellt sie spaeter per update() genau an dieser Stelle auf.
    # In `results` kommt die Runde erst, wenn beide Teile da sind (siehe Grading).
    return {"round": rnd, "run_id": RUN_ID, "build_sha256_16": build_sha, "noop": build_sha == prior_hash,
            "session_id": session["uuid"], "session_name": session["name"], "session_continued": continuing,
            "files": files, "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0_wall)),
            "exit_code": rc, "exit_meaning": EXIT_MEANING.get(rc, f"rc={rc}"), "wall_s": round(wall, 2),
            "first_write_t": round(first_write_t, 2) if first_write_t else None,
            "task_chars": len(task), "stdout_chars": len(out), "ladder_rung": None, "ladder_note": None,
            "model_metrics": metrics, "capability": None, "span": None,
            "grades": None, "grades_failed": None,
            "rejected": None,
            "screenshots": None,
            "grader_fps": None, "audio": None, "peak_state": None,
            "proxy": proxy_agg, "ended_by": ended_by}


def snapshot_dir(run_dir, rnd):
    return os.path.join(run_dir, f"r{rnd}")


def grading_part(graded, run_dir):
    """Der BEWERTUNGSTEIL eines Rundenergebnisses -- genau die Schluessel, die frueher in
    run_round unmittelbar nach grade() entstanden, mit denselben Namen und denselben Werten.

    Ausgelagert, weil die Bewertung jetzt in einem anderen Thread und Minuten spaeter laeuft
    als der Agentenlauf; die Runde bekommt sie per res.update(...) an ihre alte Stelle.
    """
    rung = graded.get("gate", graded.get("rung", 0))
    # Spec 3.5: the round reports gate, checks, capability AND the three raw gradings the
    # median came from; spec 4.3 wants each grading's seed and window in the provenance.
    # Both were computed and then dropped on the floor, so nothing downstream could say
    # whether a 3-point span came from a flaky build or from one 300 s window against two
    # 180 s ones -- or that one of the three never ran at all. A projection, not the whole
    # grading: the checks dict and the screenshot paths are already in the merged result.
    grades_raw = [{"gate": g.get("gate", 0),
                   "capability_passed": (g.get("capability") or {}).get("passed", 0),
                   "capability_total": (g.get("capability") or {}).get("total", 0),
                   "seed": g.get("seed"), "window_s": g.get("window_s"),
                   "fps": g.get("fps"), "shot_t": g.get("shot_t"),
                   "rejected": g.get("rejected") or [], "notes": g.get("notes") or []}
                  for g in graded.get("grades") or []]
    return {"ladder_rung": rung, "ladder_note": "; ".join(graded.get("notes") or []),
            "capability": graded.get("capability") or {}, "span": graded.get("span"),
            "grades": grades_raw, "grades_failed": graded.get("grades_failed", 0),
            "rejected": graded.get("rejected"),
            "screenshots": [os.path.relpath(sh, run_dir) for sh in graded.get("screenshots", [])],
            "grader_fps": graded.get("fps"), "audio": graded.get("audio"),
            "peak_state": graded.get("peak")}


class Grading:
    """Die Bewertung laeuft HINTER dem Agenten her: ein Thread je Zeile, eine Warteschlange.

    Warum. grade() nimmt den maschinenweiten flock (arena-grader.lock), weil fps im Browser
    dieser Maschine gemessen wird und zwei gleichzeitige Grader einander die CPU wegnehmen.
    Bei neun parallelen Zeilen wartete deshalb jede Runde nicht nur auf ihre eigenen drei
    Bewertungen, sondern auf die Schlange aller anderen -- und der Agent dieser Zeile tat in
    dieser ganzen Zeit nichts, obwohl sein Build als Schnappschuss laengst fertig dalag. Der
    Agent zieht jetzt weiter, sobald sein Prozess endet; bewertet wird r<n>/, das sich nicht
    mehr aendert. Der flock bleibt: es bewertet weiterhin nur einer zur Zeit, aber niemand
    haelt dafuer noch einen Agenten an.

    Die Runde kommt erst in `results` und in die Checkpoint-Datei, wenn BEIDE Teile da sind.
    Wird eine laufende Bewertung abgebrochen (Ctrl-C), fehlt ihre Runde in der Datei, statt
    halb dazustehen -- und ein spaeterer Wiedereinstieg bewertet ihren Schnappschuss nach.
    """

    def __init__(self, bench, cfg, run_dir, out_path, header, attrs, pipelined=True,
                 results=None):
        self.bench, self.cfg, self.run_dir = bench, cfg, run_dir
        self.out_path, self.header, self.attrs = out_path, header, attrs
        self.pipelined = pipelined
        self.results = list(results or [])
        self.proc = None            # der GERADE laufende Grader -- main()s finally raeumt ihn ab
        self.q = queue.Queue()
        self.lock = threading.Lock()
        # daemon: ein Ctrl-C soll den Prozess beenden koennen, auch wenn gerade bewertet wird.
        self.thread = threading.Thread(target=self._work, name="grading", daemon=True)
        self.thread.start()

    def submit(self, res, rnd):
        """Runde n zur Bewertung anmelden. Ohne Fliessband (v2-Bank ohne bench.json) wartet
        der Aufrufer hier, bis sie fertig ist -- die Reihenfolge von frueher, unveraendert."""
        self.q.put((res, rnd))
        if res.get("resumed"):
            print(f"round {rnd}: snapshot without a result -- queued for grading (resumed)", flush=True)
        else:
            print(f"round {rnd}: agent done in {int(res.get('wall_s') or 0)}s, queued for grading",
                  flush=True)
        if not self.pipelined:
            self.drain()

    def pending(self):
        return self.q.unfinished_tasks

    def drain(self):
        self.q.join()

    def _work(self):
        while True:
            res, rnd = self.q.get()
            try:
                self._grade_one(res, rnd)
            except Exception as exc:                      # eine Runde darf die Schlange nicht toeten
                print(f"round {rnd}: grading failed in the runner ({type(exc).__name__}: {exc})",
                      flush=True)
            finally:
                self.q.task_done()

    def _grade_one(self, res, rnd):
        snap = snapshot_dir(self.run_dir, rnd)
        with arena_trace.span(f"grade-{rnd}", round_no=rnd, **self.attrs) as sp:
            graded = grade(snap, self.bench, self.run_dir, rnd, self.cfg,
                           on_start=lambda proc: setattr(self, "proc", proc))
            part = grading_part(graded, self.run_dir)
            sp.set_attribute("arena_ladder_rung", part["ladder_rung"])
            sp.set_attribute("arena_capability_passed", (part["capability"] or {}).get("passed", 0))
            sp.set_attribute("arena_capability_total", (part["capability"] or {}).get("total", 0))
        res.update(part)
        with self.lock:
            self.results.append(res)
            self.results.sort(key=lambda r: r.get("round") or 0)
            self._checkpoint()
        print(f"round {rnd}: graded gate {part['ladder_rung']} "
              f"cap {(part['capability'] or {}).get('passed')} (queue {max(0, self.pending() - 1)})",
              flush=True)
        # EIN Schreibvorgang: zwei print() aus zwei Threads koennen sich ineinanderschieben,
        # und eine zerrissene JSON-Zeile in runner.log ist genau die Zeile, die spaeter
        # jemand parst.
        sys.stdout.write(json.dumps(res) + "\n"); sys.stdout.flush()

    def kill_grader(self, grace_s=10):
        """Den laufenden Grader beenden -- SIGINT an seine GRUPPE, damit grader.py sein finally
        (d.quit()) noch faehrt und geckodriver/Firefox mit heruntergehen. Aufgerufen aus main()s
        finally: ein Ctrl-C erreicht den daemon-Thread nicht, sein Kind lief sonst weiter und
        mass gegen die naechste Bewertung an."""
        proc = self.proc
        if not proc or proc.poll() is not None:
            return None
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return None
        how = kill_grader_group(pgid, proc, grace_s)
        reap_grader_group(pgid)          # und nichts aus seiner Gruppe ueberlebt ihn
        return how

    def _checkpoint(self):
        """Checkpoint nach jeder bewerteten Runde -- ueber eine temporaere Datei, weil ein
        Ctrl-C waehrend des Schreibens sonst eine halbe JSON-Datei hinterlaesst und damit
        genau den Beleg zerstoert, den der Checkpoint retten soll. fsync vor dem Umbenennen:
        os.replace ist atomar fuer den Verzeichniseintrag, nicht fuer den INHALT der neuen
        Datei -- ohne fsync kann ein Absturz die alte Datei durch eine leere ersetzen."""
        tmp = self.out_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(dict(self.header, rounds=self.results), fh, indent=2)
            fh.flush(); os.fsync(fh.fileno())
        os.replace(tmp, self.out_path)


def resumed_round(rnd, run_dir, note):
    """Der Agententeil einer Runde, deren Agentenlauf dieser Prozess nicht gesehen hat.

    Zwei Faelle: --resume-after-agent N (der Agent lief im vorigen Runner) und eine Runde,
    deren Bewertung ein Stopp mitten im Lauf abgeschnitten hat. Was aus dem Schnappschuss
    noch ablesbar ist, steht drin (Dateien, Build-Hash, Proxy-Aggregat, falls es geschrieben
    wurde); was nur der Agentenlauf wusste -- Exit-Code, Wanduhr, stdout -- ist weg und steht
    als None da, statt erfunden zu werden.
    """
    snap = snapshot_dir(run_dir, rnd)
    proxy_path = os.path.join(snap, "proxy.json")
    try:
        proxy = json.load(open(proxy_path, encoding="utf-8")) if os.path.exists(proxy_path) else None
    except Exception as exc:
        proxy = {"error": f"unreadable proxy.json: {exc}"[:200]}
    files = sorted(build_files(snap, snapshot=True))
    return {"round": rnd, "run_id": RUN_ID, "build_sha256_16": build_hash(snap, snapshot=True),
            "noop": None,
            "session_id": None, "session_name": None, "session_continued": None,
            "files": files, "started": None,
            "exit_code": None, "exit_meaning": "not observed (resumed)", "wall_s": None,
            "first_write_t": None,
            "task_chars": None, "stdout_chars": None, "ladder_rung": None, "ladder_note": None,
            "model_metrics": None, "capability": None, "span": None,
            "grades": None, "grades_failed": None,
            "rejected": None,
            "screenshots": None,
            "grader_fps": None, "audio": None, "peak_state": None,
            "proxy": proxy if proxy is not None else {"note": "not captured (resumed)"},
            "ended_by": None, "resumed": note}


def _terminate(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def install_signal_handlers():
    """SIGINT und SIGTERM muessen bei DIESEM Prozess ankommen -- explizit, nicht geerbt.

    Gemessen am 2026-09-10 an einer laufenden Zeile: `kill -INT <runner>` tat NICHTS, der
    Prozess blieb in flock() stehen, auch per tgkill an den Hauptthread. Grund: die Zeilen
    werden als Hintergrundjob einer nicht-interaktiven Shell gestartet (`nohup ... &` in
    bin/arena-contest). POSIX verlangt, dass eine Shell asynchrone Kommandos mit SIGINT und
    SIGQUIT auf SIG_IGN startet, und CPython installiert seinen eigenen SIGINT-Handler NUR,
    wenn die geerbte Disposition nicht SIG_IGN ist. Also gab es kein KeyboardInterrupt, also
    lief das finally nie -- weder der Grader-Abbau noch der Proxy-Abbau.

    SIGTERM bekommt denselben Weg: `kill` ohne Argument ist das, was ein Operator zuerst
    tippt, und die Zeile soll dabei nicht ihren Proxy mit belegtem Port hinterlassen.
    """
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _terminate)


def main():
    install_signal_handlers()
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, choices=sorted(WRAPPERS))
    ap.add_argument("--game", default="antifa_survivors")
    ap.add_argument("--rounds", type=int, default=99)
    # Ein Lauf ueber sieben Runden dauert Stunden und wurde heute viermal unterbrochen --
    # von einem Reboot, von einem Fehlgriff, und zweimal, weil jemand anders die Karte
    # brauchte. Ohne Wiedereinstieg kostet jede Unterbrechung den ganzen Lauf. Mit
    # --from-round setzt man dort fort, wo die Ergebnisdatei aufhoert; der Arbeitsstand im
    # workdir ist ja noch da (deshalb dann OHNE --fresh starten).
    ap.add_argument("--from-round", type=int, default=1,
                    help="bei dieser Runde beginnen; vorhandene Ergebnisse werden geladen")
    ap.add_argument("--timeout", type=int, default=0,
                    help="per-round seconds; 0 = no timeout (let the round finish)")
    ap.add_argument("--fresh", action="store_true", help="start from an empty workdir")
    # Der Einstiegspunkt fuer eine LAUFENDE Zeile, die auf das Fliessband umgestellt wird, und
    # fuer jeden Stopp genau zwischen zwei Runden: "Runde N hat ihren Agentenlauf hinter sich,
    # der Build im Arbeitsverzeichnis IST ihr Ergebnis". Der neue Runner macht daraus r<N>,
    # haengt es in die Bewertungsschlange und faehrt mit Runde N+1 in derselben Sitzung fort.
    # Ohne das kostet die Umstellung eine Runde: --from-round N wuerde N noch einmal bauen.
    ap.add_argument("--resume-after-agent", type=int, default=None, metavar="N",
                    help="Runde N ist gebaut, aber nicht bewertet: workdir als r<N> sichern, "
                         "bewerten und mit N+1 fortfahren (nicht mit --fresh kombinierbar)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.resume_after_agent is not None:
        # Dieselbe Falle wie --fresh + --from-round, nur schaerfer: --resume-after-agent
        # SICHERT das Arbeitsverzeichnis als Rundenergebnis, --fresh loescht es.
        if args.fresh:
            ap.error("--resume-after-agent snapshots the work directory as round N's build, "
                     "--fresh deletes it -- pick one")
        if args.resume_after_agent < 1:
            ap.error("--resume-after-agent takes the number of the round whose agent is done (>= 1)")
        if args.from_round > 1 and args.from_round != args.resume_after_agent + 1:
            ap.error(f"--resume-after-agent {args.resume_after_agent} continues at round "
                     f"{args.resume_after_agent + 1}, not at {args.from_round}")
        args.from_round = args.resume_after_agent + 1
    # --fresh wipes the workdir; --from-round resumes against the work that is still in it.
    # Together they delete exactly the build the resumed round was supposed to continue, and
    # the run reads afterwards like rounds 1..n-1 simply produced nothing.
    if args.fresh and args.from_round > 1:
        ap.error("--fresh wipes the work directory, --from-round resumes against it -- "
                 "pick one (resume without --fresh)")

    bench = os.path.join(HERE, "benchmarks", args.game)
    workdir = os.path.join(WORK_ROOT, args.game, args.config)
    if args.fresh and os.path.isdir(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)
    total = len(json.load(open(os.path.join(bench, "rounds.json"), encoding="utf-8")))
    if args.rounds > total:
        args.rounds = total
    # One directory per run. Every previous launch wrote to a single fixed path and wiped
    # snapshots first, so each run destroyed its predecessor's evidence -- which is how the
    # only valid run's graded build was lost.
    run_dir = os.path.join(bench, "runs", RUN_ID)
    os.makedirs(run_dir, exist_ok=True)
    out_path = args.out or os.path.join(run_dir, f"results_{args.config}.json")
    # A resume (--from-round n) reuses the run directory, and writing provenance.json again
    # overwrote the record of what the FIRST attempt ran against -- serving stack, hermes
    # config, proxy hash, start time. That is the one file that says what produced rounds
    # 1..n-1, so the resume gets its own file next to it instead of replacing it.
    prov_name = "provenance.json" if args.from_round <= 1 else f"provenance-resume-{args.from_round}.json"
    with open(os.path.join(run_dir, prov_name), "w") as fh:
        json.dump(provenance(args.config, args.game, RUN_ID), fh, indent=2)

    provider = arena_trace.setup(f"antifa-survivors-{args.config}")
    results = []
    if args.from_round > 1 and os.path.exists(out_path):
        # Bereits gemessene Runden uebernehmen, statt sie zu ueberschreiben: die Datei ist
        # der Beleg dieser Runden, und ein Wiedereinstieg darf ihn nicht loeschen.
        try:
            results = json.load(open(out_path)).get("rounds", [])
            results = [r for r in results if r.get("round") < args.from_round]
            print(f"resuming at round {args.from_round}; kept {len(results)} recorded round(s)",
                  flush=True)
        except Exception as exc:
            print(f"could not read {out_path} ({exc}); starting with an empty result list",
                  flush=True)

    cfg = load_bench_cfg(bench)
    header = {"run_id": RUN_ID, "config": args.config, "game": args.game,
              "model": WRAPPERS[args.config][3],
              "serving_config": os.environ.get("ARENA_SERVING_CONFIG", "setup-1"),
              "endpoint": os.environ.get("QWEN_BASE_URL", ""),
              "note": "ctx_tokens_* are chars//4 estimates, not tokenizer counts",
              "bench_cfg": cfg}
    # Fliessband nur fuer v3 (Bank MIT bench.json). Eine v2-Bank bewertet weiter Runde fuer
    # Runde, bevor der naechste Agent startet: ihre Zahlen sind unter genau dieser Reihenfolge
    # entstanden, und v2 wird nicht mehr veraendert.
    pipelined = os.path.exists(os.path.join(bench, "bench.json"))
    grading = Grading(bench, cfg, run_dir, out_path, header,
                      dict(config=os.environ.get("ARENA_SERVING_CONFIG", "setup-1"),
                           harness=WRAPPERS[args.config][2], model=WRAPPERS[args.config][3],
                           game=args.game, run_id=RUN_ID),
                      pipelined=pipelined, results=results)
    proxy_proc, proxy_port, proxy_log = None, None, None
    if args.config in PROXY:
        dialect, upstream, proxy_port = PROXY[args.config]
        proxy_log = open(os.path.join(run_dir, "proxy.log"), "a")
        proxy_proc = subprocess.Popen([sys.executable, os.path.join(HERE, "tools", "trace_proxy.py"),
                                       "--upstream", upstream, "--port", str(proxy_port), "--dialect", dialect,
                                       "--project", "arena-v3-proxy", "--row", args.config, "--run-id", RUN_ID,
                                       "--stats-dir", run_dir], stdout=proxy_log, stderr=subprocess.STDOUT)
        time.sleep(1.0)
        if proxy_proc.poll() is not None:
            sys.exit(f"proxy for {args.config} died at start (port {proxy_port} busy?) -- see {run_dir}/proxy.log")
    elif cfg["proxy_required"]:
        sys.exit(f"{args.game} requires a proxy but {args.config} has no PROXY entry")
    try:
        session = session_for(RUN_ID)
        if args.resume_after_agent:
            # Der Build im Arbeitsverzeichnis IST Runde N -- sichern, falls der vorige Runner
            # nicht mehr dazu kam. Ist r<N> schon da (er kam dazu), bleibt es, wie es ist:
            # der Schnappschuss ist der Beleg, das Arbeitsverzeichnis ist nur sein Original.
            snap = snapshot_dir(run_dir, args.resume_after_agent)
            if not os.path.exists(os.path.join(snap, "index.html")):
                shutil.copytree(workdir, snap, ignore=shutil.ignore_patterns(".*"), dirs_exist_ok=True)
                print(f"round {args.resume_after_agent}: snapshotted the work directory as "
                      f"r{args.resume_after_agent}", flush=True)
        # Jede Runde vor dem Einstieg, die einen Schnappschuss, aber kein Ergebnis hat, wird
        # nachbewertet. Das ist der Normalfall nach --resume-after-agent und der Rettungsfall
        # nach einem Stopp, der eine laufende Bewertung abgeschnitten hat.
        have = {r.get("round") for r in results}
        for rnd in range(1, args.from_round):
            # index.html, nicht nur das Verzeichnis: r<k>/ entsteht schon VOR dem Agentenlauf.
            # Ein vertipptes --resume-after-agent wuerde sonst ein leeres Verzeichnis bewerten
            # und eine erfundene Runde mit Gate 0 in die Ergebnisdatei schreiben.
            if rnd in have or not os.path.exists(os.path.join(snapshot_dir(run_dir, rnd), "index.html")):
                continue
            grading.submit(resumed_round(
                rnd, run_dir, "graded on resume; this runner did not run the agent"), rnd)
        aborted_at = None
        for rnd in range(args.from_round, args.rounds + 1):
            continuing = cfg["session_mode"] == "continue" and rnd > 1
            res = run_round(args.config, args.game, rnd, workdir, bench, args.timeout, run_dir,
                            cfg, proxy_port, session, continuing)
            res["session_fallback"] = False
            if res["exit_code"] == 9:
                # Sitzung nicht fortsetzbar: EINMAL frisch mit vollem Prompt, neuer Name (Spec 2.5).
                # Der erste (verworfene) Versuch bleibt erhalten -- sein stdout waere sonst vom
                # zweiten Aufruf ueberschrieben (beide schreiben stdout-r<rnd>.txt), und ohne
                # seine Kennzahlen im Protokoll sieht die Runde aus wie ein sauberer Erstversuch.
                print(f"round {rnd}: session not resumable -- falling back to a fresh session", flush=True)
                first = res
                first_out = os.path.join(run_dir, f"stdout-r{rnd}.txt")
                if os.path.exists(first_out):
                    shutil.move(first_out, os.path.join(run_dir, f"stdout-r{rnd}-attempt1.txt"))
                # Move the discarded attempt's proxy calls out of this round's tag before the
                # retry runs: both attempts POSTed {"round": rnd}, so r<n>/proxy.json summed
                # them and the round's calls, tokens and inflight_max described two agent runs
                # while every other field described one. The discarded attempt keeps its
                # numbers under rnd*100+1 (/__arena/stats?round=<that>) instead of losing them.
                if proxy_port:
                    try:
                        urllib_post(f"http://127.0.0.1:{proxy_port}/__arena/round",
                                    {"round": rnd, "retag_from": rnd, "retag_to": rnd * 100 + 1})
                    except Exception as exc:
                        print(f"round {rnd}: proxy retag of the discarded attempt failed ({exc})", flush=True)
                session = session_for(RUN_ID, fallback_round=rnd)
                res = run_round(args.config, args.game, rnd, workdir, bench, args.timeout, run_dir,
                                cfg, proxy_port, session, False)
                res["session_fallback"] = True
                res["session_fallback_first_attempt"] = {"exit_code": first["exit_code"],
                                                          "exit_meaning": first["exit_meaning"],
                                                          "wall_s": first["wall_s"],
                                                          "proxy_round_tag": rnd * 100 + 1,
                                                          "proxy": first.get("proxy")}
            # Der Schnappschuss liegt schon (run_round); ab hier gehoert die Runde dem
            # Bewertungsthread. Die Steuerentscheidungen unten warten NICHT auf ihn -- sie
            # haengen nur am Exit-Code des Agenten, der hier bereits feststeht.
            grading.submit(res, rnd)
            # Only a dead endpoint stops the run. A harness failure (8) is recorded and
            # the next round is attempted: the wrappers used to map both to 4, so one bad
            # spawn discarded every remaining round and looked like a dead model host.
            if res["exit_code"] == 4:
                print("endpoint unreachable -- stopping", flush=True)
                break
            if res["exit_code"] == 8:
                print(f"round {rnd}: harness failure -- recorded, continuing", flush=True)
            # Aarons Regel: eine Zeile ist mit ihrer ersten abgebrochenen Runde beendet.
            # Bisher stand sie nur im Wachauftrag, d.h. die Zeile lief weiter, bis ein
            # Mensch (oder ein 30-Minuten-Poll) sie stoppte -- und produzierte in der
            # Zwischenzeit Runden, die ohnehin nicht gewertet werden. ARENA_STOP_ON_ABORT=1
            # zieht die Regel in den Runner: der Abbruch wird protokolliert, die bereits
            # bewerteten Runden bleiben vollstaendig, und die Kette darf weiterlaufen.
            # Standard bleibt AUS, damit ein Nachlauf einer fertigen Zeile sich nicht
            # ploetzlich anders verhaelt als der Lauf, mit dem er verglichen wird.
            abort = abort_reason(res)
            if abort and os.environ.get("ARENA_STOP_ON_ABORT") == "1":
                print(f"round {rnd}: {abort} -- row ends here (ARENA_STOP_ON_ABORT); "
                      f"rounds {rnd + 1}-{args.rounds} not attempted", flush=True)
                aborted_at = {"round": rnd, "reason": abort,
                              "not_attempted": list(range(rnd + 1, args.rounds + 1))}
                break
        # Erst wenn kein Agent mehr laeuft, wird auf die Schlange gewartet -- und zwar HIER,
        # nicht im finally: ein Ctrl-C soll den Abbau (Proxy beenden) sofort erreichen und
        # darf nicht noch minutenlang auf eine laufende Bewertung warten. Die abgebrochene
        # Runde fehlt dann in der Ergebnisdatei; --resume-after-agent bewertet sie nach.
        if grading.pending():
            print(f"all agents done -- waiting for {grading.pending()} round(s) to be graded",
                  flush=True)
        # VOR drain(): der letzte Checkpoint des Bewertungsthreads schreibt den Kopf mit, und
        # dann steht der Abbruchgrund schon in der Ergebnisdatei, falls der Prozess danach
        # stirbt. Der explizite Checkpoint darunter ist fuer den Fall, dass drain() gar nichts
        # mehr zu tun hat und deshalb nie schreibt.
        if aborted_at:
            grading.header["aborted_at"] = aborted_at
        grading.drain()
        if aborted_at:
            grading._checkpoint()
    finally:
        # Zuerst der Grader, dann der Proxy. Der Bewertungsthread ist ein daemon: ein Ctrl-C
        # erreicht ihn nie, und sein Grader-Kind lief bisher mit geckodriver und Firefox
        # weiter -- gegen die Bewertung der naechsten Zeile, also gegen genau die fps-Zahl,
        # fuer die es den Lock gibt.
        killed = grading.kill_grader()
        if killed:
            print(f"grading in flight: grader {killed} (its round has no result)", flush=True)
        # Den Proxy VOR provider.force_flush() beenden: haengt oder wirft force_flush() (OTLP-Export nach
        # Phoenix), ueberlebt der Proxy sonst mit belegtem Port, und der naechste Lauf stirbt
        # schon beim Start ("port busy?"). Ein Messgeraet-Fehler darf den naechsten Lauf nicht
        # kosten.
        if proxy_proc and proxy_proc.poll() is None:
            proxy_proc.terminate()      # per PID -- nie pkill
            try: proxy_proc.wait(5)
            except subprocess.TimeoutExpired: proxy_proc.kill()
        if proxy_log:
            proxy_log.close()
        try:
            provider.force_flush(timeout_millis=5000)
        except Exception as exc:
            print(f"provider.force_flush() failed ({exc}) -- traces for this run may be incomplete", flush=True)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()

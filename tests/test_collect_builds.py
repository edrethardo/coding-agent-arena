import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import collect_builds as C


def test_copy_round_copies_every_file_of_a_multifile_round():
    """A v3 round is multi-file: index.html + game.js + css/style.css. Copying only
    index.html serves a page whose relative <script src>/<link href> resolve to nothing --
    a blank page. copy_round() must bring every regular file across, recursively."""
    bench_root = tempfile.mkdtemp()
    run_dir = os.path.join(bench_root, "testgame", "runs", "V3-run--row")
    round_dir = os.path.join(run_dir, "r1")
    os.makedirs(os.path.join(round_dir, "css"))
    open(os.path.join(round_dir, "index.html"), "w").write("<html><script src=game.js></script></html>")
    open(os.path.join(round_dir, "game.js"), "w").write("// game")
    open(os.path.join(round_dir, "css", "style.css"), "w").write("body{}")
    # a dotfile must NOT be served
    open(os.path.join(round_dir, ".hidden"), "w").write("nope")

    dst = os.path.join(tempfile.mkdtemp(), "served", "r1")
    C.copy_round(round_dir, dst)

    assert os.path.exists(os.path.join(dst, "index.html"))
    assert os.path.exists(os.path.join(dst, "game.js"))
    assert os.path.exists(os.path.join(dst, "css", "style.css"))
    assert not os.path.exists(os.path.join(dst, ".hidden"))


def test_copy_round_skips_agent_scratch_files():
    round_dir = tempfile.mkdtemp()
    open(os.path.join(round_dir, "index.html"), "w").write("<html></html>")
    open(os.path.join(round_dir, "_test_harness.js"), "w").write("/home/user/secret")
    dst = tempfile.mkdtemp()
    C.copy_round(round_dir, dst)
    assert os.path.exists(os.path.join(dst, "index.html"))
    assert not os.path.exists(os.path.join(dst, "_test_harness.js"))


def test_rewrite_public_markdown_drops_tailnet_and_machine_names():
    src = ("Stand: https://edrethardo.github.io/antifa-survivors-arena\n"
           "Analytics: https://edrethardo.github.io/antifa-survivors-arena/analytics.html\n"
           "Gemessen auf local GPU host (RTX 3090)\n")
    out = C.rewrite_public_markdown(src)
    assert "taild" not in out
    assert "./" in out
    assert "analytics.html" in out


def test_copy_round_matches_old_behaviour_for_a_v2_round():
    """A v2 round's directory holds only index.html -- copy_round() must serve exactly
    that, unchanged, same as the old shutil.copy(index.html) it replaces."""
    round_dir = tempfile.mkdtemp()
    open(os.path.join(round_dir, "index.html"), "w").write("<html>v2</html>")

    dst = tempfile.mkdtemp()
    C.copy_round(round_dir, dst)

    assert sorted(os.listdir(dst)) == ["index.html"]
    assert open(os.path.join(dst, "index.html")).read() == "<html>v2</html>"


def test_front_page_is_v3_only_older_runs_on_archiv():
    """The front page (index.html) shows only the v3 series; antifa_survivors_v2 (and any
    other pre-v3 game) moves to archiv.html. Run against the REAL repo, not a synthetic
    fixture -- this repo already has real antifa_survivors_v3 and antifa_survivors_v2
    data, which is exactly the mix this has to split correctly."""
    import subprocess
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root = tempfile.mkdtemp()
    cb = os.path.join(repo, "tools", "collect_builds.py")
    subprocess.run([sys.executable, cb, repo, root, "/arena"], check=True,
                   capture_output=True, text=True)
    index = open(os.path.join(root, "index.html")).read()
    archiv = open(os.path.join(root, "archiv.html")).read()

    assert "builds/antifa_survivors_v2/" not in index
    assert "builds/antifa_survivors_v2/" in archiv

    # podium candidates on the front page are v3 only
    if '<section id="podium"' in index:
        podium = index[index.index('<section id="podium"'):index.index("</section>", index.index('<section id="podium"'))]
        assert "antifa_survivors_v2" not in podium
        assert "builds/antifa_survivors_v2/" not in podium

    # the tab bar on each page links to the other and to analytics.html
    assert 'href="archiv.html"' in index and 'href="analytics.html"' in index
    assert 'href="index.html"' in archiv and 'href="analytics.html"' in archiv


def test_analytics_pages_split_v3_from_older_runs():
    """analytics.html (the v3 traces/analytics tab) mentions only v3 run ids; the older
    runs' throughput/gantt/title-wall/trace-link content moves to archiv-analytics.html.
    Uses the real repo's own run ids -- not hardcoded -- so the test tracks whatever runs
    actually exist, the same mix test_front_page_is_v3_only_older_runs_on_archiv checks
    for the builds pages."""
    import subprocess
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root = tempfile.mkdtemp()
    cb = os.path.join(repo, "tools", "collect_builds.py")
    subprocess.run([sys.executable, cb, repo, root, "/arena"], check=True,
                   capture_output=True, text=True)

    all_runs = C.analytics_page(repo, "/arena", "")
    v2_run_ids = [run for game, run, prov, rounds in all_runs if game == "antifa_survivors_v2"]
    v3_run_ids = [run for game, run, prov, rounds in all_runs if C.is_v3_game(game)]
    assert v2_run_ids, "expected at least one antifa_survivors_v2 run in this repo"
    assert v3_run_ids, "expected at least one v3 run in this repo"

    analytics = open(os.path.join(root, "analytics.html")).read()
    archiv = open(os.path.join(root, "archiv.html")).read()
    archiv_analytics = open(os.path.join(root, "archiv-analytics.html")).read()

    for run in v2_run_ids:
        assert run not in analytics, f"v2 run {run} leaked onto analytics.html"
        assert run in archiv_analytics, f"v2 run {run} missing from archiv-analytics.html"
    for run in v3_run_ids:
        assert run in analytics, f"v3 run {run} missing from analytics.html"

    # both archive pages link to each other and back to the front page
    assert 'href="archiv-analytics.html"' in archiv
    assert 'href="archiv.html"' in archiv_analytics
    assert 'href="index.html"' in archiv_analytics

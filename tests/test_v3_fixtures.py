import os, subprocess, sys
FX = os.path.join(os.path.dirname(__file__), "..", "benchmarks", "antifa_survivors_v3", "fixtures")
NAMES = ["good", "touch", "pointer", "mouse", "frozen", "fake-victory", "slow", "multi", "silent", "no-title"]

def test_generator_writes_all_fixtures(tmp_path):
    subprocess.run([sys.executable, os.path.join(FX, "make_fixtures.py"), str(tmp_path)], check=True)
    for n in NAMES:
        assert os.path.exists(tmp_path / f"fx-{n}" / "index.html"), n
    assert os.path.exists(tmp_path / "fx-multi" / "game.js")
    assert os.path.exists(tmp_path / "fx-multi" / "style.css")
    assert "game.js" not in open(tmp_path / "fx-good" / "index.html").read()

def test_input_variants_differ_only_in_fx_line(tmp_path):
    subprocess.run([sys.executable, os.path.join(FX, "make_fixtures.py"), str(tmp_path)], check=True)
    a = open(tmp_path / "fx-touch" / "index.html").read().splitlines()
    b = open(tmp_path / "fx-pointer" / "index.html").read().splitlines()
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert len(diff) == 1 and "window.FX" in a[diff[0]]

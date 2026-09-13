import json, os, re
B = os.path.join(os.path.dirname(__file__), "..", "benchmarks", "antifa_survivors_v3")

def test_rounds_are_seven():
    r = json.load(open(os.path.join(B, "rounds.json")))
    assert len(r) == 7
    assert "title screen" in r[0].lower()
    assert "waveshaper" in r[1].lower()

def test_base_discloses_no_metric():
    t = open(os.path.join(B, "base.txt")).read()
    for banned in ("240", "0-24", "0-6", "weapons_4", "fps60", "checklist", "rung"):
        assert banned not in t, banned
    assert re.search(r"\b4\+ weapons|\b8\+ weapons|\b12 weapons", t) is None
    for required in ("index.html", "WaveShaperNode", "170", "title screen", "__state", "Read what you need", "same session"):
        assert required in t, required

def test_bench_json():
    c = json.load(open(os.path.join(B, "bench.json")))
    assert c["paste_prior_file"] is False
    assert c["session_mode"] == "continue"
    assert c["grade_repeats"] == 3
    assert c["window_s"] == [180, 300]
    assert c["proxy_required"] is True
    assert "grader_sha256" in c

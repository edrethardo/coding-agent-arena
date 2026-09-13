"""Collect every graded build in the repo into one servable tree, one URL per round.

Layout on disk mirrors the layout in the record, so a URL is traceable back to its
evidence without a lookup table:

    <root>/builds/<game>/<run_id>/r<n>/index.html

Each build is copied, never symlinked: a run directory is the evidence behind a published
number, and a served page must not be able to change when that directory is tidied.

Scores come from the run's own results JSON. They are shown next to each round because a
build served without its score invites the reader to judge it by eye, which is the thing
this benchmark exists to avoid.
"""
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

import v3_analytics as v3


# Aufkleber-Motive, wie sie tatsaechlich auf Laptopdeckeln und Laternen kleben. Jedes
# Motiv hat seine eigene Form -- FCK NZS ist ein schwarzer Block mit zwei Zeilen, die
# Antifaschistische Aktion ein Kreis mit zwei Fahnen, "Good Night White Pride" ein runder
# Button. Reine Textrahmen sahen wie Etiketten aus, nicht wie Sticker.
def sticker_layer():
    """Rein dekorative Ebene: Laptopdeckel voller Aufkleber.

    Positionen sind fest verdrahtet statt zufaellig, damit die Seite bei jedem Neubau
    gleich aussieht -- ein Hintergrund, der bei jedem Refresh springt, wirkt kaputt.
    `aria-hidden` und `pointer-events:none`: Screenreader und Maus sollen ihn nicht sehen.
    """
    def block(l1, l2, tone="blk"):
        return f'<b class="st blk {tone}"><i>{l1}</i><i>{l2}</i></b>'

    def tape(txt, tone="red"):
        return f'<b class="st tape {tone}">{txt}</b>'

    def round_(txt, tone="blk"):
        return f'<b class="st circ {tone}"><span>{txt}</span></b>'

    def flag():
        # Antifaschistische Aktion: zwei Fahnen im Kreis, angedeutet durch zwei
        # gegeneinander gekippte Balken -- die Form ist erkennbar, ohne Bilddatei.
        return ('<b class="st circ aa"><u class="f1"></u><u class="f2"></u>'
                '<span>ANTIFASCHISTISCHE AKTION</span></b>')

    motifs = [
        block("FCK", "NZS"), tape("KEIN MENSCH IST ILLEGAL", "red"),
        round_("GOOD NIGHT WHITE PRIDE"), flag(),
        tape("NO PASAR\u00c1N", "red"), block("NAZIS", "RAUS", "red"),
        tape("REFUGEES WELCOME", "blue"), round_("ALERTA ANTIFASCISTA", "red"),
        block("KEIN", "FUSSBREIT"), tape("NIE WIEDER", "yel"),
        block("FCK", "AFD"), tape("SOLIDARIT\u00c4T STATT HETZE", "blue"),
        round_("KEIN ORT F\u00dcR NAZIS"), tape("LOVE MUSIC HATE FASCISM", "yel"),
        block("BLOCK", "THE FASH"), flag(),
        tape("HALTUNG ZEIGEN", "red"), block("KEIN", "VERGESSEN"),
        round_("KIEZ BLEIBT SOLIDARISCH", "red"), tape("WEHRET DEN ANF\u00c4NGEN", "yel"),
    ]
    # Positionen liegen bewusst in den AUSSENBAENDERN (links unter 13 %, rechts ueber
    # 74 %) und beginnen unterhalb der Kopfleiste. Die Inhaltsspalte ist zentriert und
    # hoechstens 74rem breit; Aufkleber darin sassen hinter dem Text und haben ihn
    # schwerer lesbar gemacht. Auf schmalen Geraeten deckt die Spalte alles ab -- dort
    # traegt die niedrigere Deckkraft die Lesbarkeit.
    spots = [(1, 16, -9), (77, 13, 4), (2, 30, -4), (86, 27, 8), (4, 44, -7),
             (79, 41, 6), (0, 57, -12), (88, 54, 3), (3, 70, -6),
             (76, 67, -5), (1, 83, 9), (85, 80, -8), (5, 96, 4),
             (80, 93, 7), (2, 109, -6), (87, 106, 5), (4, 122, -4),
             (78, 119, -8), (1, 135, 6), (84, 132, -5)]
    out = ['<div class="stickers" aria-hidden="true">']
    for m, (l, t, rot) in zip(motifs, spots):
        out.append(m.replace('class="st ', f'style="left:{l}%;top:{t}%;--r:{rot}deg" class="st ', 1))
    out.append("</div>")
    return "".join(out)


# Drei Pfeile im Kreis -- das Zeichen der Eisernen Front, seit den 1930ern das
# antifaschistische Symbol schlechthin, und damit das passende Wappen fuer eine Arena, in
# der Agenten "Antifa Survivors" bauen. Daneben ein blinkender Cursor: es bleibt eine
# Coding-Arena. Inline-SVG statt Bilddatei, damit die Seite eine einzige Datei bleibt und
# das Zeichen in jeder Groesse scharf ist.
LOGO_SVG = (
    # Geometrie geprueft: der Versatz laeuft senkrecht zur Pfeilrichtung, und alle drei
    # Pfeile liegen mitsamt Strichbreite innerhalb r=13.5. Ein erster Entwurf versetzte
    # um 4,5 in x UND y -- das sind 6,4 quer zur Richtung, und die aeusseren Pfeile
    # ragten sichtbar aus dem Kreis.
    '<svg viewBox="0 0 32 32" width="30" height="30" aria-hidden="true" class="logo">'
    '<circle cx="16" cy="16" r="13.5" fill="none" stroke="currentColor" stroke-width="2"/>'
    '<g stroke="currentColor" stroke-width="2.4" stroke-linecap="square" fill="none">'
    '<g transform="translate(-3.2,-3.2)">'
    '<path d="M22 9 L13 18"/><path d="M13 18 L13 14"/><path d="M13 18 L17 18"/></g>'
    '<g><path d="M22 9 L13 18"/><path d="M13 18 L13 14"/><path d="M13 18 L17 18"/></g>'
    '<g transform="translate(3.2,3.2)">'
    '<path d="M22 9 L13 18"/><path d="M13 18 L13 14"/><path d="M13 18 L17 18"/></g>'
    '</g></svg>')

FAVICON = (
    "data:image/svg+xml;utf8,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='4' fill='%230a0b0f'/%3E"
    "%3Ccircle cx='16' cy='16' r='13' fill='none' stroke='%23ff2e88' stroke-width='2'/%3E"
    "%3Cg stroke='%2300ff9c' stroke-width='2.6' fill='none' stroke-linecap='square'%3E"
    "%3Cg transform='translate(-3.2,-3.2)'%3E"
    "%3Cpath d='M22 9 L13 18'/%3E%3Cpath d='M13 18 L13 14'/%3E%3Cpath d='M13 18 L17 18'/%3E%3C/g%3E"
    "%3Cg%3E%3Cpath d='M22 9 L13 18'/%3E%3Cpath d='M13 18 L13 14'/%3E%3Cpath d='M13 18 L17 18'/%3E%3C/g%3E"
    "%3Cg transform='translate(3.2,3.2)'%3E"
    "%3Cpath d='M22 9 L13 18'/%3E%3Cpath d='M13 18 L13 14'/%3E%3Cpath d='M13 18 L17 18'/%3E%3C/g%3E"
    "%3C/g%3E%3C/svg%3E")


def vote_script(vote_repo):
    """Stimmen liegen als 👍-Reaktionen an GitHub-Issues -- eine je Kandidat.

    GitHub Pages liefert nur statische Dateien, es gibt also keinen Server, der zaehlen
    koennte. Diese Loesung braucht dafuer keine eigene Infrastruktur und kein Geheimnis:
    Anmeldung, Missbrauchsschutz und "eine Stimme je Konto" uebernimmt GitHub. Die Seite
    liest die Zaehler ueber die oeffentliche API (ohne Token, 60 Anfragen je Stunde und
    IP), deshalb genau EIN Aufruf je Seitenaufruf, zwischengespeichert in sessionStorage.

    Faellt die API aus -- Ratelimit, Repo noch leer, offline --, bleiben die Knoepfe
    stehen und nur die Zaehler fehlen. Eine Seite, die ohne Netz kaputtgeht, waere ein
    schlechter Tausch fuer eine Zahl.
    """
    if not vote_repo:
        return ""
    return r"""<script>
(function(){
  var REPO=%r, KEY='arena-votes-'+REPO;
  function paint(map){
    document.querySelectorAll('[data-vote]').forEach(function(el){
      var n=map[el.getAttribute('data-vote')];
      var b=el.querySelector('.cnt');
      if(b) b.textContent = (n===undefined?'':n);
      if(n>0) el.classList.add('has');
    });
    var rank=Object.keys(map).sort(function(a,b){return map[b]-map[a];});
    var box=document.getElementById('voteboard');
    if(box&&rank.length){
      box.innerHTML=rank.slice(0,5).map(function(k,i){
        return '<li><span>'+(i+1)+'</span><b>'+k+'</b><i>'+map[k]+'</i></li>';}).join('');
    }
  }
  try{var c=sessionStorage.getItem(KEY); if(c){paint(JSON.parse(c)); return;}}catch(e){}
  fetch('https://api.github.com/repos/'+REPO+'/issues?labels=vote&state=all&per_page=100')
    .then(function(r){ if(!r.ok) throw 0; return r.json(); })
    .then(function(list){
      var map={};
      list.forEach(function(i){
        var m=/^\[vote\]\s*(.+)$/.exec(i.title||'');
        if(m) map[m[1].trim()]=(i.reactions&&i.reactions['+1'])||0;
      });
      try{sessionStorage.setItem(KEY,JSON.stringify(map));}catch(e){}
      paint(map);
    })
    .catch(function(){ /* Zaehler bleiben leer, Knoepfe funktionieren weiter */ });
})();
</script>""" % vote_repo


def vote_button(vote_repo, row):
    if not vote_repo:
        return ""
    q = urllib.parse.quote(f"[vote] {row}")
    url = (f"https://github.com/{vote_repo}/issues?q=" + q)
    return (f'<a class="btn vote" data-vote="{html.escape(row)}" href="{html.escape(url)}" '
            f'target="_blank" rel="noopener">&#9733; STIMMEN <span class="cnt"></span></a>')


def tabs(active, mount):
    """Reiterleiste. `active` ist "builds", "archiv" oder "analytics"."""
    def t(key, href, label):
        cls = "tab on" if key == active else "tab"
        return f'<a class="{cls}" href="{href}">{label}</a>'
    return ('<div class="tabs">'
            + t("builds", "index.html", "BUILDS")
            + t("bericht", "bericht.html", "BERICHT")
            + t("analytics", "analytics.html", "TRACES &amp; ANALYTICS")
            + t("archiv", "archiv.html", "&Auml;LTERE L&Auml;UFE")
            + '</div>')


def sub_tabs(active, mount):
    """Zweite, schmalere Reiterleiste -- nur auf den beiden Archiv-Seiten. Die drei
    Haupt-Reiter (BUILDS/&Auml;LTERE L&Auml;UFE/TRACES & ANALYTICS) fuehren zwischen
    Startseite, Archiv-Bauten und der v3-Analytics; sie sagen aber nicht, wie man von den
    Archiv-Bauten zu IHRER EIGENEN Analytics kommt. `active` ist "archiv" oder
    "archiv-analytics"."""
    def t(key, href, label):
        cls = "tab on" if key == active else "tab"
        return f'<a class="{cls}" href="{href}">{label}</a>'
    return ('<div class="tabs sub">'
            + t("archiv", "archiv.html", "ARCHIV BUILDS")
            + t("archiv-analytics", "archiv-analytics.html", "ANALYTICS &Auml;LTERE L&Auml;UFE")
            + '</div>')


def analytics_page(repo, mount, phoenix_url):
    """Zweite Seite: was die Laeufe gekostet haben und was die Spuren sagen.

    Liest ausschliesslich die aufgezeichneten Rundenergebnisse. Zahlen, die eine
    Einschraenkung tragen (Wallzeit, Prefix-Cache), sind hier mit ihr beschriftet -- sonst
    liest sie jemand als Leistungsmass, was sie nicht sind.
    """
    runs = []
    bench_root = os.path.join(repo, "benchmarks")
    for game in sorted(os.listdir(bench_root)) if os.path.isdir(bench_root) else []:
        rdir = os.path.join(bench_root, game, "runs")
        if not os.path.isdir(rdir):
            continue
        for run in sorted(os.listdir(rdir), reverse=True):
            d = os.path.join(rdir, run)
            prov = {}
            pf = os.path.join(d, "provenance.json")
            if os.path.exists(pf):
                try:
                    prov = json.load(open(pf))
                except Exception:
                    pass
            for f in glob.glob(os.path.join(d, "results_*.json")):
                try:
                    data = json.load(open(f))
                except Exception:
                    continue
                if data.get("rounds"):
                    runs.append((game, run, prov, data["rounds"]))
    return runs


def gate_max(res):
    """Wie viele Sprossen hat die Leiter dieser Runde? v2: 6. v3: 5 (Spec 3.2).

    Jede Gate-Zahl auf der Seite stand als "n/6" und wurde erst bei 6 gruen -- eine v3-Zeile
    mit dem hoechsten erreichbaren Gate 5 las sich als "5/6", also als knapp verfehlt, und
    bekam nie die gruene Farbe. Ein perfekter v3-Build sah dauerhaft schlechter aus als ein
    perfekter v2-Build. Erkannt wird v3 an denselben Schluesseln, an denen die Analytics-
    Tabelle ihre v3-Zusatzzeile erkennt: `span`, `noop` und `proxy` schreibt nur der
    v3-Runner."""
    res = res or {}
    return 5 if any(k in res for k in ("span", "noop", "proxy")) else 6


def is_v3_game(game):
    """Is `game` (a benchmarks/<game>/ folder name) part of the v3 series?

    Matched as a "_v3" token, not a fixed name list, so a v3 variant folder (e.g. a smoke
    test alongside the real campaign) is automatically included without a code change --
    both share the same v3 grader/spec and neither belongs with the older v1/v2 games on
    the archive page. Token match (split on "_"), not a bare substring, so a hypothetical
    future game whose name merely contains "v3" as part of a longer word is not
    misclassified."""
    return "v3" in game.split("_")


def setup_line(run_dir):
    """Eine Zeile, die sagt, WELCHES Setup diesen Lauf erzeugt hat.

    Ohne sie stehen auf der Seite Builds nebeneinander, die sich in Modell, Harness,
    Kontextfenster oder Kontextfuehrung unterscheiden, und nichts sagt einem, worin. Genau
    das ist der Zweck der A/B-Zeilen, also gehoert es sichtbar an den Build.
    """
    p = os.path.join(run_dir, "provenance.json")
    if not os.path.exists(p):
        return ""
    try:
        d = json.load(open(p))
    except Exception:
        return ""
    bits = []
    if d.get("harness"):
        bits.append(d["harness"])
    if d.get("model_declared"):
        bits.append(d["model_declared"])
    if d.get("backend") == "local" and d.get("max_model_len_reported"):
        bits.append(f"ctx {int(d['max_model_len_reported']):,}".replace(",", "."))
    hc = d.get("harness_config") or {}
    prune = hc.get("proactive_prune_tokens")
    if prune is not None:
        bits.append("prune " + ("aus" if prune in ("0", 0) else str(prune)))
    if hc.get("max_attempts") and hc["max_attempts"] not in ("3",):
        bits.append(f"attempts {hc['max_attempts']}")
    if d.get("agent_sandbox") and d["agent_sandbox"] != "on":
        bits.append(f"sandbox {d['agent_sandbox']}")
    return " · ".join(bits)


def copy_round(src_dir, dst_dir):
    """Copy every regular, non-dotfile under `src_dir` into `dst_dir`, recursively,
    preserving the relative directory structure.

    A v2/v1 round is one file, `index.html`. A v3 round is multi-file: `index.html` plus
    its own `game.js`/`style.css` (relative `<script src>`/`<link href>` inside it expect
    siblings, not a lone copied `index.html`) plus `grade-*/` screenshot subdirectories.
    Copying the whole directory is a strict superset of "copy index.html" -- a v2 round,
    which has nothing else in its directory, is copied identically to before.

    `node_modules` is the one exception. An agent may install a dev-time test harness for
    itself -- the 131k row pulled in jsdom, 27 MB in 1830 files -- and none of it is part
    of the delivered game: the spec forbids build tools and runtime fetches, so the page
    only ever needs index.html and the agent's own scripts. Copied, it would put 27 MB on
    the page PER ROUND and bury the actual build in a directory listing. Skipped by name
    rather than by size, because the point is what it IS, not how big it grew.
    """
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        rel = os.path.relpath(root, src_dir)
        out_dir = dst_dir if rel == "." else os.path.join(dst_dir, rel)
        os.makedirs(out_dir, exist_ok=True)
        for f in files:
            if f.startswith(".") or f.startswith("_"):
                # Dotfiles stay private. Underscore-prefixed files are agent scratch
                # (`_test_harness.js`) and have leaked workstation paths onto the page.
                continue
            shutil.copy(os.path.join(root, f), os.path.join(out_dir, f))


def find_builds(repo):
    """Every round build, from all three layouts the runs have used."""
    out = []
    bench_root = os.path.join(repo, "benchmarks")
    for game in sorted(os.listdir(bench_root)) if os.path.isdir(bench_root) else []:
        runs = os.path.join(bench_root, game, "runs")
        if not os.path.isdir(runs):
            continue
        for run in sorted(os.listdir(runs)):
            rd = os.path.join(runs, run)
            results = {}
            for f in os.listdir(rd):
                if f.startswith("results_") and f.endswith(".json"):
                    try:
                        results = json.load(open(os.path.join(rd, f)))
                    except Exception:
                        results = {}
            by_round = {r.get("round"): r for r in results.get("rounds", [])}
            setup = setup_line(rd)

            # v2: runs/<run>/r<n>/index.html
            for name in sorted(os.listdir(rd)):
                p = os.path.join(rd, name, "index.html")
                if name.startswith("r") and name[1:].isdigit() and os.path.exists(p):
                    out.append(dict(game=game, run=run, label=name, rnd=int(name[1:]),
                                    src=p, res=by_round.get(int(name[1:]), {}), kind="round", setup=setup))
            # v1: runs/<run>/builds/<row>-r<n>/index.html
            bdir = os.path.join(rd, "builds")
            if os.path.isdir(bdir):
                for name in sorted(os.listdir(bdir)):
                    p = os.path.join(bdir, name, "index.html")
                    if os.path.exists(p):
                        n = int(name.rsplit("-r", 1)[-1]) if "-r" in name else 0
                        out.append(dict(game=game, run=run, label=f"r{n}", rnd=n, src=p,
                                        res=by_round.get(n, {}), kind="round", setup=setup))
            # salvage: an artefact, explicitly not a result
            p = os.path.join(rd, "salvage", "index.html")
            if os.path.exists(p):
                grade = {}
                gp = os.path.join(rd, "salvage", "grade.json")
                if os.path.exists(gp):
                    try:
                        grade = json.load(open(gp))
                    except Exception:
                        pass
                out.append(dict(game=game, run=run, label="salvage", rnd=0, src=p,
                                res={"ladder_rung": grade.get("rung"),
                                     "capability": grade.get("capability", {}),
                                     "grader_fps": grade.get("fps")},
                                kind="salvage", setup=setup))
    return out


def score_cell(b):
    """The score, labelled by the ladder that produced it.

    v1's rung and v2's gate are both 0-6 and mean different things: v1 awarded rung 4 for
    one kill in 45 s, v2 stops the gate at the first rung not earned and measures input
    under ?autoplay=0. Printing both as "gate n/6" in one list invites exactly the
    comparison the method documents forbid -- run E's 4 next to run G's 2 reads as a
    regression and is not one. The presence of a capability checklist is what tells the
    two apart, because only v2's grader produces one.
    """
    res = b["res"] or {}
    rung = res.get("ladder_rung")
    cap = (res.get("capability") or {})
    is_v2 = bool(cap.get("total"))
    gmax = gate_max(res)
    bits = []
    if rung is not None:
        bits.append(f"gate {rung}/{gmax}" if is_v2 else f"v1 rung {rung}/6")
    if cap.get("total"):
        bits.append(f"capability {cap.get('passed')}/{cap['total']}")
    if res.get("grader_fps"):
        bits.append(f"{res['grader_fps']} fps")
    if res.get("wall_s"):
        bits.append(f"{res['wall_s'] / 60:.0f} min")
    return " · ".join(bits) or "not scored"


PUBLIC = False
VOTE_REPO = ""


def rewrite_public_markdown(md: str) -> str:
    """Strip workstation and tailnet addresses from BERICHT.md for GitHub Pages."""
    md = re.sub(
        r"https://dust\.taild[0-9a-z]*\.ts\.net/arena(?:/([A-Za-z0-9._-]+\.html))?",
        lambda m: m.group(1) or "./",
        md,
        flags=re.I,
    )
    return md.replace("local GPU host", "local RTX 3090 host").replace(
        "local GPU host", "local RTX 3090 host")


def main():
    global PUBLIC, VOTE_REPO
    repo, root, mount = sys.argv[1], sys.argv[2], sys.argv[3]
    # Oeffentliche Fassung (GitHub Pages): relative Basis, keine internen Verweise,
    # Abstimmung ueber GitHub-Issues. Der Tailnet-Betrieb bleibt unveraendert.
    PUBLIC = os.environ.get("ARENA_PUBLIC") == "1"
    VOTE_REPO = os.environ.get("ARENA_VOTE_REPO", "")
    if PUBLIC:
        mount = "./"
    builds = find_builds(repo)
    rows = []
    for b in builds:
        dest_dir = os.path.join(root, "builds", b["game"], b["run"], b["label"])
        if b["kind"] == "round":
            # A v3 round is multi-file (index.html + game.js + style.css + grade-*/
            # screenshots) -- copying only index.html serves a page whose relative
            # <script src>/<link href> resolve to nothing, i.e. a blank page. Copying the
            # whole round directory covers game.js/style.css AND the screenshots in one
            # pass (their recorded path, e.g. "r3/grade-0/title.png", is already relative
            # to the run dir, i.e. lands right under this same round's copied directory),
            # so the separate screenshot-only copy this used to need is gone. A v2/v1
            # round's directory has nothing but index.html, so it is copied identically
            # to before.
            copy_round(os.path.dirname(b["src"]), dest_dir)
        else:
            # salvage: an artefact, not a round -- keep the old, narrower behaviour (only
            # index.html) so grade.json, sitting right next to it, is not newly exposed.
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy(b["src"], os.path.join(dest_dir, "index.html"))
        rel = f"builds/{b['game']}/{b['run']}/{b['label']}/index.html"
        # v3: `href`/`run_rel` are the same values chip() computes inline for its own
        # link/autoplay hrefs, saved once on the build so title_wall() (and anything
        # else outside main()) can address this build without recomputing them.
        b["href"] = rel
        b["run_rel"] = f"builds/{b['game']}/{b['run']}"
        rows.append((b, rel))

    # A contest is several rows launched together under one label: their run ids share a
    # prefix and differ after "--". Rendering those as separate blocks would bury the only
    # question they exist to answer -- how the same round differs between models -- so a
    # contest becomes one table: a row per round, a column per model, a link in every cell.
    v3_rows = [(b, rel) for b, rel in rows if is_v3_game(b["game"])]
    archive_rows = [(b, rel) for b, rel in rows if not is_v3_game(b["game"])]
    index_parts = render_builds_page(repo, root, mount, v3_rows, "index.html", "builds",
                                     "Arena builds", "Arena builds")
    render_builds_page(repo, root, mount, archive_rows, "archiv.html", "archiv",
                       "Arena builds \u2014 \u00c4ltere L\u00e4ufe", "\u00c4ltere L\u00e4ufe")

    # --- zweite Seite: Traces & Analytics -----------------------------------------
    phoenix = "" if PUBLIC else os.environ.get("ARENA_PHOENIX_URL", "")
    all_runs = analytics_page(repo, mount, phoenix)
    style = index_parts[0][index_parts[0].index("<style>"):index_parts[0].index("</style>") + 8]
    v3_runs = [r for r in all_runs if is_v3_game(r[0])]
    archive_runs = [r for r in all_runs if not is_v3_game(r[0])]
    v3_builds = [b for b, _ in v3_rows]
    archive_builds = [b for b, _ in archive_rows]
    render_analytics_page(repo, root, mount, v3_builds, v3_runs, style, phoenix,
                          "analytics.html", "analytics", "Arena \u2014 Traces & Analytics",
                          "Traces & Analytics")
    render_analytics_page(repo, root, mount, archive_builds, archive_runs, style, phoenix,
                          "archiv-analytics.html", "archiv-analytics",
                          "Arena \u2014 Analytics \u00e4ltere L\u00e4ufe",
                          "Analytics \u00e4ltere L\u00e4ufe")
    if not render_report_page(repo, root, mount, style):
        print("warning: BERICHT.md missing -- the BERICHT tab points nowhere")
    print(f"collected {len(rows)} build(s) into {root}")


def render_report_page(repo, root, mount, style):
    """Vierter Reiter: der geschriebene Bericht, aus BERICHT.md gerendert.

    Die Seite hat bisher Bauten, Zahlen und Spuren gezeigt -- aber nicht die Auswertung.
    Wer nur den Link bekommt, soll das Ergebnis lesen koennen, ohne ins Repo zu steigen.
    Quelle ist genau die Datei, die auch im Repo liegt; es gibt keine zweite Fassung, die
    auseinanderlaufen koennte. Fehlt sie, entfaellt die Seite (und der Reiter zeigt ins
    Leere statt auf eine Luege -- deshalb meldet main() das auf stdout)."""
    src = os.path.join(repo, "benchmarks", "antifa_survivors_v3", "BERICHT.md")
    if not os.path.exists(src):
        return False
    md = open(src, encoding="utf-8").read()
    if PUBLIC:
        md = rewrite_public_markdown(md)
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        # Ohne Renderer lieber den Rohtext ausliefern als den Reiter ins Leere zeigen zu
        # lassen: lesbar bleibt er, nur unformatiert.
        body = "<pre class=\"rawmd\">" + html.escape(md) + "</pre>"
    else:
        # "gfm-like" schaltet Tabellen und durchgestrichenen Text frei (linkify bleibt aus:
        # linkify-it-py ist nicht installiert, und <...>-Autolinks kann der Kern selbst); der Bericht ist
        # eine Tabellenwueste, ohne das waere er unlesbar. html=False laesst rohes HTML
        # aus der Quelle NICHT durch -- die Datei ist unser eigener Text, aber die Regel
        # kostet nichts und haelt die Seite frei von eingeschleustem Markup.
        body = MarkdownIt("gfm-like", {"html": False, "linkify": False}).render(md)
    rp = [f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="{html.escape(mount.rstrip('/'))}/">
<title>Arena \u2014 Bericht</title>\n<link rel="icon" href="{FAVICON}">
{style}
<style>
 .rep{{max-width:56rem}}
 .rep h1{{font-size:1.6rem;margin:1.4rem 0 .6rem;color:var(--neon)}}
 .rep h2{{font-size:1.25rem;margin:2.2rem 0 .6rem;padding-top:1.2rem;
          border-top:1px solid var(--line)}}
 .rep h3{{font-size:1.05rem;margin:1.6rem 0 .5rem;color:#cbd3e6}}
 .rep p,.rep li{{line-height:1.65;max-width:46rem}}
 .rep table{{border-collapse:collapse;margin:1rem 0;font-size:.86rem;
             display:block;overflow-x:auto;max-width:100%}}
 .rep th,.rep td{{border:1px solid var(--line);padding:.3rem .55rem;text-align:left;
                  white-space:nowrap}}
 .rep th{{background:#161b26;color:#9aa5bd;font-weight:600}}
 .rep tr:nth-child(even) td{{background:#12161f}}
 .rep code{{background:#161b26;padding:.1rem .3rem;border-radius:2px;font-size:.85em}}
 .rep pre.rawmd{{white-space:pre-wrap;font-size:.82rem;line-height:1.5}}
 .rep hr{{border:0;border-top:1px solid var(--line);margin:2rem 0}}
 .rep blockquote{{border-left:2px solid var(--neon);margin:1rem 0;padding:.2rem 0 .2rem 1rem;
                  color:#9aa5bd}}
 .rep a{{color:var(--neon)}}
</style>
<header><div class="wrap">
<h1 id="top">{LOGO_SVG}<span>Bericht</span></h1>
<p class="lead">Die Auswertung der v3-Kampagne &mdash; was die Agenten taugen und welche
Konfiguration das lokale Modell tr&auml;gt. Gerendert aus
<code>benchmarks/antifa_survivors_v3/BERICHT.md</code>.</p>
{tabs("bericht", mount)}
</div></header>
<div class="wrap"><article class="rep">"""]
    rp.append(body)
    rp.append("</article></div>")
    with open(os.path.join(root, "bericht.html"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(rp))
    return True


def render_analytics_page(repo, root, mount, builds, runs, style, phoenix, out_name, tab_key, page_title, h1_text):
    subtab_html = sub_tabs("archiv-analytics", mount) if tab_key == "archiv-analytics" else ""
    ap = [f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="{html.escape(mount.rstrip('/'))}/">
<title>{page_title}</title>\n<link rel="icon" href="{FAVICON}">
{style}
<header><div class="wrap">
<h1 id="top">{LOGO_SVG}<span>{h1_text}</span></h1>
<p class="lead">Was die L&auml;ufe gekostet haben, gelesen aus den aufgezeichneten
Rundenergebnissen. <b>Wallzeit und Prefix-Cache sind keine Leistungsma&szlig;e</b> &mdash;
sie messen Parallellast und Serverw&auml;rme mit.</p>
{tabs(tab_key, mount)}{subtab_html}
</div></header>
<div class="wrap">"""]

    # v3: throughput, timeline and title wall read only what runs already carry (proxy
    # aggregates, started/wall_s, screenshots) -- a v2 run supplies none of it, so these
    # sections shrink to their empty-state text and change nothing for v2.
    tp_rows = v3.throughput_rows(runs)
    if tp_rows:
        ap.append(f'<section><h2>Durchsatz</h2>{v3.throughput_table(tp_rows)}</section>')
    ap.append(f'<section><h2>Zeitstrahl</h2>{v3.gantt(runs)}</section>')
    wall = v3.title_wall(builds)
    if wall:
        ap.append(f'<section><h2>Titelbild-Wand</h2>{wall}</section>')

    tot_rounds = sum(len(r[3]) for r in runs)
    tot_prompt = sum((x.get("model_metrics") or {}).get("prompt_tokens", 0) or 0
                     for r in runs for x in r[3])
    tot_gen = sum((x.get("model_metrics") or {}).get("generation_tokens", 0) or 0
                  for r in runs for x in r[3])
    tot_h = sum((x.get("wall_s") or 0) for r in runs for x in r[3]) / 3600
    ap.append('<section><h2>Gesamt</h2><div class="kpis">'
              f'<div class="kpi"><b>{len(runs)}</b><span>L&auml;ufe</span></div>'
              f'<div class="kpi"><b>{tot_rounds}</b><span>Runden</span></div>'
              f'<div class="kpi"><b>{tot_h:,.1f}</b><span>Stunden Agentenzeit</span></div>'
              f'<div class="kpi"><b>{tot_prompt/1e6:,.1f} M</b><span>Prompt-Token</span></div>'
              f'<div class="kpi"><b>{tot_gen/1e6:,.2f} M</b><span>erzeugte Token</span></div>'
              '</div></section>')

    for game, run, prov, rounds in runs:
        su = []
        if prov.get("harness"):
            su.append(prov["harness"])
        if prov.get("model_declared"):
            su.append(prov["model_declared"])
        hc = prov.get("harness_config") or {}
        if prov.get("backend") == "local" and prov.get("max_model_len_reported"):
            su.append(f"ctx {prov['max_model_len_reported']:,}".replace(",", "."))
        if hc.get("proactive_prune_tokens") is not None:
            su.append("prune " + ("aus" if hc["proactive_prune_tokens"] in ("0", 0)
                                  else str(hc["proactive_prune_tokens"])))
        su_html = (f'<p class="setup">{html.escape(" · ".join(su))}</p>' if su else "")
        ap.append(f'<section><h2>{html.escape(run)}</h2>{su_html}'
                  '<div class="scroll"><table><tr>'
                  '<th>R</th><th>gate</th><th>cap</th><th title="Wallzeit -- misst Parallellast mit">min*</th>'
                  '<th>req</th><th>prompt-tok</th><th>gen-tok</th><th>ø gen/req</th>'
                  '<th title="haengt von Serverwaerme und Session-Alter ab">cache*</th>'
                  '<th>exit</th><th>Build</th></tr>')
        prev = None
        run_dir = os.path.join(repo, "benchmarks", game, "runs", run)
        run_rel = f"builds/{game}/{run}"
        for r in rounds:
            m = r.get("model_metrics") or {}
            c = r.get("capability") or {}
            rq = m.get("requests") or 0
            sha = r.get("build_sha256_16")
            same = " <i class=noop>unver&auml;ndert</i>" if (sha and sha == prev) else ""
            prev = sha or prev
            err = m.get("metrics_error") or m.get("metrics_note")
            cells = (f'<td>{rq or "—"}</td><td>{m.get("prompt_tokens",0):,}</td>'
                     f'<td>{m.get("generation_tokens",0):,}</td>'
                     f'<td>{(m.get("generation_tokens",0)//rq if rq else 0):,}</td>'
                     f'<td>{m.get("prefix_cache_hit_rate","—")}</td>') if not err else \
                    f'<td colspan="5" class="dim">{html.escape(str(err)[:60])}</td>'
            ex = r.get("exit_meaning", "")
            excls = "ok" if ex == "ok" else "bad"
            # v3: a graded round is checked `span` times; `capability.passed` is the
            # recorded (last) value, `span` the [min, max] seen across those gradings.
            span = r.get("span")
            if span and len(span) == 2 and span[0] != span[1]:
                cap_html = f'{c.get("passed","—")} <sup>{span[0]}&ndash;{span[1]}</sup>'
            else:
                cap_html = f'{c.get("passed","—")}'
            is_noop = bool(r.get("noop"))
            cap_attrs = ' class="cap-noop" title="unver&auml;ndert zur Vorrunde"' if is_noop else ""
            ap.append(f'<tr><td>{r["round"]}</td>'
                      f'<td>{r.get("ladder_rung")}/{gate_max(r)}</td><td{cap_attrs}>{cap_html}/24</td>'
                      f'<td>{(r.get("wall_s") or 0)/60:.0f}</td>{cells}'
                      f'<td class="{excls}">{html.escape(ex)}</td>'
                      f'<td class="dim">{(sha or "—")[:8]}{same}</td></tr>')
            # v3: a screenshot strip (title/t120) and the proxy event strip, one extra
            # full-width row directly under the round it belongs to. `screenshots`,
            # `span`, `noop` and `proxy` are all v3-only keys -- any of them present
            # marks this as a v3 round; a v2 round has none and gets no extra row.
            if any(k in r for k in ("screenshots", "span", "noop", "proxy")):
                shots = r.get("screenshots") or []
                imgs = "".join(
                    f'<img src="{html.escape(run_rel + "/" + s)}" height="120" '
                    f'alt="{html.escape(os.path.basename(s))}">'
                    for s in shots if s.endswith("title.png") or s.endswith("t120.png"))
                shots_html = f'<div class="shots">{imgs}</div>' if imgs else ""
                ap.append(f'<tr><td colspan="11">{shots_html}'
                          f'{v3.event_strip(run_dir, r.get("round"), r)}</td></tr>')
        ap.append('</table></div></section>')

    traces = ['<section><h2>Traces</h2><p class="setup">ein Span je Runde, Projekt '
              '<code>antifa-survivors-&lt;zeile&gt;</code></p>']
    if phoenix:
        traces.append(
            f'<p class="legend">Phoenix l&auml;uft intern und ist nur im '
            f'Tailnet erreichbar: <a class="btn play" style="display:inline-flex;'
            f'padding:.4rem .8rem" href="{html.escape(phoenix)}">PHOENIX &Ouml;FFNEN</a><br><br>')
    else:
        traces.append('<p class="legend">')
    traces.append(
        'Span-Attribute je Runde: <code>arena_run_id</code>, <code>arena_config</code>, '
        '<code>arena_harness</code>, <code>arena_model</code>, <code>arena_round</code>, '
        '<code>arena_exit_code</code>, <code>arena_wall_s</code>, '
        '<code>arena_ladder_rung</code>, <code>arena_capability_passed</code> sowie '
        'ein <code>arena_&lt;feld&gt;</code> je Modell-Metrik.<br><br>'
        '<b>*</b> Wallzeit und Prefix-Cache tragen Einschr&auml;nkungen: die eine misst '
        'die gleichzeitige Last mit (2 bis 15 parallele Runden), die andere die '
        'Serverw&auml;rme und das Alter der Session. Beide stehen hier als Kontext, '
        'nicht als Ergebnis.</p></section></div>')
    ap.extend(traces)

    with open(os.path.join(root, out_name), "w") as fh:
        fh.write("\n".join(ap))


def render_builds_page(repo, root, mount, rows_subset, out_name, tab_key, page_title, h1_text):
    contests, singles = {}, {}
    for b, rel in rows_subset:
        if "--" in b["run"]:
            stamp, row = b["run"].split("--", 1)
            contests.setdefault((b["game"], stamp), {}).setdefault(row, []).append((b, rel))
        else:
            singles.setdefault((b["game"], b["run"]), []).append((b, rel))

    def anchor(text):
        return "s-" + "".join(c if c.isalnum() else "-" for c in text).strip("-").lower()

    def strip_and_badge(items):
        """Ein Verlaufsstreifen und die Endnote.

        Sieben Runden als sieben Bloecke, eingefaerbt nach Gate. Ein Zusammenbruch ist damit
        auf einen Blick zu sehen, ohne Zahlen zu lesen -- das ist die Frage, die man an so
        eine Liste zuerst stellt: hat es gehalten oder nicht?
        """
        rounds = [b for b, _ in items if b["kind"] == "round"]
        rounds.sort(key=lambda b: b["rnd"])
        blocks = []
        for b in rounds:
            res = b["res"] or {}
            g = res.get("ladder_rung")
            gm = gate_max(res)
            cls = "u" if g is None else ("g" if g >= gm else ("y" if g >= 3 else "r"))
            blocks.append(f'<i class="{cls}" title="R{b["rnd"]}: gate {g}/{gm}"></i>')
        if not rounds:
            return "", ""
        last = rounds[-1]["res"] or {}
        cap = last.get("capability") or {}
        g = last.get("ladder_rung")
        peak = rounds[-1].get("res", {}).get("peak_state") or {}
        badge = ""
        gm = gate_max(last)
        if g is not None and cap.get("total"):
            tone = "g" if g >= gm else ("y" if g >= 3 else "r")
            end = ""
            if peak.get("ended") == "victory":
                end = " · Sieg"
            badge = (f'<span class="badge {tone}">{g}/{gm} &middot; {cap.get("passed")}/24'
                     f'{end}</span>')
        return '<span class="strip">' + "".join(blocks) + "</span>", badge

    def chip(b, rel, label=None):
        """Eine Runde als Karte mit ZWEI beschrifteten Knoepfen.

        Vorher war die Karte selbst der Spielen-Link und daneben stand ein nacktes Dreieck.
        Auf dem Handy war weder erkennbar, dass die Karte klickbar ist, noch was das
        Dreieck tut. Beschriftete Knoepfe kosten etwas Platz und nehmen die Rateaufgabe weg.
        """
        res = b["res"] or {}
        cap = res.get("capability") or {}
        gate = res.get("ladder_rung")
        v2 = bool(cap.get("total"))
        head = label or b["label"]
        if gate is None:
            score, cls = "nicht bewertet", "n"
        elif not v2:
            score, cls = f"v1 rung {gate}/6", "v1"
        else:
            gm = gate_max(res)
            score = f"{gate}/{gm} · {cap.get('passed')}/24"
            cls = "hi" if gate >= gm else ("lo" if gate <= 2 else "mid")
        play = html.escape(rel + "?autoplay=0")
        watch = html.escape(rel + "?autoplay=1")
        warn = ""
        if b["kind"] == "salvage":
            warn = '<div class="warn">Bergung, kein Rundenergebnis</div>'
        elif not v2 and gate is not None:
            warn = '<div class="warn">v1-Leiter, nicht mit v2 vergleichbar</div>'
        return (f'<div class="card {cls}">'
                f'<div class="hd"><span class="rd">{html.escape(head)}</span>'
                f'<span class="sc">{html.escape(score)}</span></div>{warn}'
                f'<div class="btns"><a class="btn play" href="{play}">SPIELEN</a>'
                f'<a class="btn auto" href="{watch}">&#9654; AUTOPLAY</a></div></div>')

    # Neueste zuerst: der laufende Versuch steht oben, das Archiv darunter. Games sort
    # first (descending), so a later ladder series (e.g. antifa_survivors_v3) renders as
    # its own block above an earlier one (antifa_survivors_v2) instead of interleaving by
    # timestamp -- v3's gate/checklist are not v2's, so mixing their rows together would
    # invite exactly the cross-version comparison the method forbids (see score_cell()).
    contest_keys = sorted(contests, key=lambda k: (k[0], k[1]), reverse=True)
    single_keys = sorted(singles, key=lambda k: (k[0], k[1]), reverse=True)

    def plural(n, one, many):
        return f"{n} {one if n == 1 else many}"

    # Welche Zeilen arbeiten gerade? Ohne das liegt die juengste Aktivitaet irgendwo mitten
    # auf der Seite -- eine neu gestartete Zeile kann unter einer alten Wettlauf-Kennung
    # haengen, damit sie neben ihrem Vergleichspartner steht, und ist dann drei Abschnitte
    # tief vergraben. Gemessen am eigenen Leib: eine frisch gestartete Zeile galt als
    # "nicht da", weil niemand bis dorthin scrollt.
    def running_rows():
        try:
            out = subprocess.run(["pgrep", "-af", "run_game_bench[.]py"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return set()
        return {m.group(1) for m in re.finditer(r"--config\s+(\S+)", out)}

    active = running_rows()
    live = []
    for (game, stamp), by_row in contests.items():
        for row, items in by_row.items():
            if row in active:
                live.append((stamp, row, items))
    for (game, run), items in singles.items():
        if run in active:
            live.append((run, run, items))

    nav = ([("live", "L\u00e4uft gerade", f"{len(live)}")] if live else []) \
        + [("podium", "Spitzenreiter", "top 3")]
    for game, stamp in contest_keys:
        nav.append((anchor(stamp), stamp, plural(len(contests[(game, stamp)]), "Zeile", "Zeilen")))
    for game, run in single_keys:
        nav.append((anchor(run), run, plural(len(singles[(game, run)]), "Build", "Builds")))

    n_builds = len(rows_subset)
    n_runs = len(contest_keys) + len(single_keys)
    parts = [f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="{html.escape(mount.rstrip('/'))}/">
<title>{page_title}</title>\n<link rel="icon" href="{FAVICON}">
<style>
 /* Bewusst EIN Erscheinungsbild, hell wie dunkel: das Blatt setzt seine Farben selbst.
    Kontraste sind gegen den dunklen Grund geprueft -- Neon ist Akzent, nie Fliesstext. */
 :root{{--bg:#0a0b0f;--panel:#12141b;--panel2:#171a23;--line:#242938;--fg:#d7dbe3;
        --dim:#79839a;--neon:#00ff9c;--mag:#ff2e88;--cy:#22d3ee;--amber:#ffc14d;
        --mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,Consolas,monospace}}
 *{{box-sizing:border-box}}
 html{{background:var(--bg)}}
 body{{margin:0;color:var(--fg);background:
      radial-gradient(1200px 600px at 12% -10%,rgba(0,255,156,.07),transparent 60%),
      radial-gradient(900px 500px at 90% 0%,rgba(255,46,136,.06),transparent 55%),
      var(--bg);
      font:15px/1.55 var(--mono);letter-spacing:.01em;
      background-attachment:fixed}}
 body::before{{content:"";position:fixed;inset:0;pointer-events:none;z-index:1;opacity:.16;
      background:repeating-linear-gradient(180deg,rgba(255,255,255,.05) 0 1px,transparent 1px 3px)}}
 .wrap{{max-width:74rem;margin:0 auto;padding:0 1rem 5rem;position:relative;z-index:2}}
 header{{position:sticky;top:0;z-index:6;
      background:linear-gradient(180deg,rgba(10,11,15,.97),rgba(10,11,15,.86));
      backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:.85rem 0 0}}
 header .wrap{{padding-bottom:0}}
 h1{{display:flex;align-items:center;gap:.6rem;font-size:1rem;margin:0 0 .25rem;
     text-transform:uppercase;letter-spacing:.22em;color:var(--neon);
     text-shadow:0 0 14px rgba(0,255,156,.45)}}
 h1 .logo{{flex:0 0 auto;color:var(--neon);
     filter:drop-shadow(0 0 6px rgba(0,255,156,.55))}}
 h1 .logo circle{{stroke:var(--mag);filter:drop-shadow(0 0 5px rgba(255,46,136,.5))}}
 h1 span{{display:inline-block}}
 h1 span::after{{content:"";display:inline-block;width:.5rem;height:1em;margin-left:.35rem;
     background:var(--mag);vertical-align:-.15em;animation:blink 1.2s steps(1) infinite}}
 @keyframes blink{{50%{{opacity:0}}}}
 @media(prefers-reduced-motion:reduce){{ h1 span::after{{animation:none}} }}
 .lead{{color:var(--dim);font-size:.8rem;margin:0 0 .7rem}}
 .lead b{{color:var(--cy);font-weight:600}}
 nav{{display:flex;gap:.4rem;overflow-x:auto;padding-bottom:.75rem;scrollbar-width:thin}}
 nav a{{flex:0 0 auto;padding:.3rem .7rem;border:1px solid var(--line);border-radius:2px;
        text-decoration:none;color:var(--dim);font-size:.75rem;white-space:nowrap;
        background:var(--panel);transition:.12s}}
 nav a:hover,nav a:focus{{border-color:var(--neon);color:var(--neon);
        box-shadow:0 0 0 1px rgba(0,255,156,.25),0 0 12px rgba(0,255,156,.18)}}
 nav a small{{color:#4d566b;margin-left:.4rem}}
 section{{margin:2.4rem 0 0;scroll-margin-top:9rem}}
 h2{{font-size:.9rem;margin:0 0 .1rem;font-weight:600;color:var(--fg);
     text-transform:uppercase;letter-spacing:.12em}}
 h2::before{{content:"// ";color:var(--mag)}}
 p.setup{{color:var(--dim);font-size:.75rem;margin:.15rem 0 .85rem;word-break:break-word}}
 p.setup::before{{content:"$ ";color:var(--neon)}}
 .rowhead{{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap;margin:1.25rem 0 .15rem}}
 .rowlab{{font-size:.82rem;font-weight:600;color:var(--cy);letter-spacing:.04em;
          text-decoration:none;scroll-margin-top:9rem}}
 .rowlab:hover{{color:var(--neon)}}
 .rowhead{{scroll-margin-top:9rem}}
 .strip{{display:inline-flex;gap:3px}}
 .strip i{{width:.8rem;height:.8rem;border-radius:1px;display:block}}
 .strip i.g{{background:var(--neon);box-shadow:0 0 8px rgba(0,255,156,.55)}}
 .strip i.y{{background:var(--amber);box-shadow:0 0 8px rgba(255,193,77,.45)}}
 .strip i.r{{background:var(--mag);box-shadow:0 0 8px rgba(255,46,136,.5)}}
 .strip i.u{{background:#232838}}
 .badge{{font-size:.72rem;padding:.14rem .55rem;border-radius:2px;white-space:nowrap;
         border:1px solid currentColor;letter-spacing:.06em}}
 .badge.g{{color:var(--neon)}} .badge.y{{color:var(--amber)}} .badge.r{{color:var(--mag)}}
 .chips{{display:grid;gap:.55rem;grid-template-columns:repeat(auto-fill,minmax(13.5rem,1fr))}}
 .card{{border:1px solid var(--line);border-radius:3px;padding:.6rem .65rem .55rem;
        background:linear-gradient(180deg,var(--panel2),var(--panel))}}
 .card .hd{{display:flex;align-items:baseline;justify-content:space-between;gap:.5rem}}
 .card .rd{{font-weight:800;font-size:.9rem;letter-spacing:.1em;text-transform:uppercase}}
 .card .sc{{font-size:.78rem;color:var(--dim);white-space:nowrap}}
 .card.hi .sc{{color:var(--neon)}} .card.lo .sc{{color:var(--mag)}}
 .card.mid .sc{{color:var(--amber)}} .card.v1 .sc{{color:var(--cy)}}
 .card .warn{{color:var(--amber);font-size:.68rem;line-height:1.3;margin-top:.25rem}}
 /* Zwei gleich breite, deutlich beschriftete Knoepfe. Mindesthoehe fuer den Daumen. */
 .btns{{display:grid;grid-template-columns:1fr 1fr;gap:.35rem;margin-top:.5rem}}
 .btn{{display:flex;align-items:center;justify-content:center;min-height:2.35rem;
       border:1px solid var(--line);border-radius:2px;text-decoration:none;
       font-size:.72rem;font-weight:700;letter-spacing:.08em;background:var(--panel);
       color:var(--fg);transition:.12s}}
 .btn.play{{border-color:rgba(0,255,156,.45);color:var(--neon)}}
 .btn.play:hover,.btn.play:focus{{background:rgba(0,255,156,.10);
       box-shadow:0 0 14px rgba(0,255,156,.18)}}
 .btn.auto{{border-color:rgba(255,46,136,.45);color:var(--mag)}}
 .btn.auto:hover,.btn.auto:focus{{background:rgba(255,46,136,.10);
       box-shadow:0 0 14px rgba(255,46,136,.18)}}
 @media(max-width:26rem){{ .chips{{grid-template-columns:1fr}} }}
 .tabs{{display:flex;gap:.3rem;margin:0 0 .7rem}}
 .tab{{padding:.4rem .9rem;border:1px solid var(--line);border-bottom:none;
       border-radius:3px 3px 0 0;text-decoration:none;color:var(--dim);font-size:.75rem;
       font-weight:700;letter-spacing:.1em;background:var(--panel)}}
 .tab.on{{color:var(--neon);border-color:rgba(0,255,156,.5);background:var(--panel2)}}
 .tab:hover{{color:var(--cy)}}
 .kpis{{display:grid;gap:.5rem;grid-template-columns:repeat(auto-fit,minmax(8rem,1fr));
        margin:.6rem 0}}
 .kpi{{border:1px solid var(--line);border-radius:3px;padding:.7rem .8rem;
       background:linear-gradient(180deg,var(--panel2),var(--panel))}}
 .kpi b{{display:block;font-size:1.4rem;color:var(--cy);line-height:1.1}}
 .kpi span{{color:var(--dim);font-size:.72rem}}
 .scroll{{overflow-x:auto}}
 table{{border-collapse:collapse;width:100%;font-size:.76rem;min-width:44rem}}
 th,td{{text-align:right;padding:.32rem .5rem;border-bottom:1px solid var(--line);
        white-space:nowrap}}
 th:first-child,td:first-child{{text-align:left}}
 th{{color:var(--dim);font-weight:600;text-transform:uppercase;letter-spacing:.06em;
     font-size:.68rem}}
 td.ok{{color:var(--neon)}} td.bad{{color:var(--mag)}} td.dim{{color:var(--dim)}}
 i.noop{{color:var(--amber);font-style:normal}}
 .legend{{color:var(--dim);font-size:.76rem;margin:2.5rem 0 0;border-top:1px solid var(--line);
          padding-top:.85rem}}
 .legend b{{color:var(--fg)}} .legend code{{color:var(--cy)}}
 .empty{{color:var(--dim);font-style:italic}}
 /* Sticker-Wand: echte Aufkleberformen statt Textrahmen. Fixiert, hinter allem, ohne
    Mausfang. Deckkraft niedrig -- Dekoration darf den Fliesstext nicht angreifen. */
 .stickers{{position:absolute;inset:0;z-index:0;pointer-events:none;overflow:hidden;
            opacity:.17;user-select:none;height:100%}}
 .stickers .st{{position:absolute;transform:rotate(var(--r));transform-origin:center;
    font-family:var(--mono);font-weight:800;text-transform:uppercase;
    box-shadow:0 2px 0 rgba(0,0,0,.5)}}
 /* Blockaufkleber: schwarze Flaeche, zwei Zeilen, sehr fett -- die FCK-NZS-Form. */
 .stickers .blk{{display:flex;flex-direction:column;line-height:.92;padding:.3rem .5rem;
    background:#0d0d10;color:#f2f4f8;border:2px solid #f2f4f8;letter-spacing:.02em;
    font-size:clamp(.85rem,2vw,1.35rem)}}
 .stickers .blk.red{{background:#c0142b;border-color:#0d0d10;color:#fff}}
 .stickers .blk i{{font-style:normal}}
 /* Klebeband-Streifen: Parole in einer Zeile auf Farbflaeche. */
 .stickers .tape{{display:block;padding:.28rem .6rem;font-size:clamp(.6rem,1.3vw,.9rem);
    letter-spacing:.08em;color:#fff;background:#0d0d10}}
 .stickers .tape.red{{background:#c0142b}}
 .stickers .tape.blue{{background:#1b4fd8}}
 .stickers .tape.yel{{background:#f2c400;color:#0d0d10}}
 /* Runder Button. */
 .stickers .circ{{display:flex;align-items:center;justify-content:center;text-align:center;
    width:5.6rem;height:5.6rem;border-radius:50%;background:#0d0d10;color:#f2f4f8;
    border:3px solid #f2f4f8;padding:.4rem;position:relative;overflow:hidden}}
 .stickers .circ.red{{background:#c0142b;border-color:#0d0d10}}
 .stickers .circ span{{font-size:.5rem;line-height:1.15;letter-spacing:.04em}}
 /* Antifaschistische Aktion: zwei gegeneinander gekippte Fahnen im Kreis. */
 .stickers .circ.aa{{background:#f2f4f8;border-color:#0d0d10}}
 .stickers .circ.aa span{{color:#0d0d10;position:relative;z-index:2;
    align-self:flex-end;font-size:.42rem}}
 .stickers .circ.aa u{{position:absolute;width:1.5rem;height:2.9rem;top:.45rem;
    display:block;transform:skewX(-16deg)}}
 .stickers .circ.aa .f1{{background:#0d0d10;left:1.1rem}}
 .stickers .circ.aa .f2{{background:#c0142b;left:2.6rem}}
 @media(max-width:34rem){{ .stickers{{opacity:.13}}
   .stickers .circ{{width:4.2rem;height:4.2rem}} }}
 /* Podium */
 .podium{{display:grid;gap:.7rem;grid-template-columns:1fr}}
 .pod{{position:relative;border:1px solid var(--line);border-radius:3px;padding:.9rem 1rem .8rem;
       background:linear-gradient(180deg,var(--panel2),var(--panel))}}
 .pod .rank{{position:absolute;top:.5rem;right:.7rem;font-size:2.4rem;font-weight:800;
             line-height:1;opacity:.18}}
 .pod .who{{font-size:.95rem;font-weight:700;letter-spacing:.05em;color:var(--cy)}}
 .pod .score{{font-size:1.25rem;font-weight:800;margin:.15rem 0 .45rem;letter-spacing:.04em}}
 .pod .cfg{{color:var(--dim);font-size:.72rem;margin:.5rem 0 .6rem;word-break:break-word}}
 .pod .acts{{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap}}
 .pod .acts a{{padding:.28rem .6rem;border:1px solid var(--line);border-radius:2px;
    text-decoration:none;color:var(--fg);font-size:.75rem;background:var(--panel)}}
 .pod .acts a:hover{{border-color:var(--cy);color:var(--cy)}}
 .pod .acts .rd{{color:#4d566b;font-size:.72rem;margin-left:auto}}
 .pod .shared{{color:var(--amber);font-size:.7rem;font-weight:600;letter-spacing:.06em}}
 .pod.p1{{border-color:var(--neon);box-shadow:0 0 0 1px rgba(0,255,156,.18),0 0 26px rgba(0,255,156,.10)}}
 .pod.p1 .score,.pod.p1 .rank{{color:var(--neon)}}
 .pod.p2{{border-color:var(--cy)}} .pod.p2 .score,.pod.p2 .rank{{color:var(--cy)}}
 .pod.p3{{border-color:var(--amber)}} .pod.p3 .score,.pod.p3 .rank{{color:var(--amber)}}
 @media(min-width:52rem){{ .podium{{grid-template-columns:repeat(3,1fr)}} }}
 @media(min-width:48rem){{ .chips{{grid-template-columns:repeat(auto-fill,minmax(12.5rem,1fr))}} }}
 @media(prefers-reduced-motion:no-preference){{ a.chip,a.pl,nav a{{transition:.12s ease}} }}
 /* v3: event strip, throughput table, title wall. The strip container is named
    `.evstrip`, not `.strip` -- `.strip` above is already the round-badge dot row
    (inline-flex); reusing the name here would have bled `position:relative;height:14px`
    into that unrelated element. */
 .evstrip{{position:relative;height:14px;background:#222;margin:4px 0}}
 .ev{{position:absolute;top:0;width:6px;height:14px}}
 .ev.compaction{{background:#ffd400}}
 .ev.http{{background:#e0202f;color:#fff;font-size:9px;width:auto;padding:0 2px}}
 .ev.exit{{background:#fff;color:#000;font-size:9px;width:auto}}
 /* erstes Schreiben einer Datei (Spec 6, Quelle Runner) -- gruen, schmal, damit es sich von
    den Proxy-Markern unterscheidet, die aus einer anderen Uhr stammen */
 .ev.write{{background:#3ddc84;width:3px}}
 .tp tr.warn td{{background:#3a2f00}}
 .titlewall{{display:flex;flex-wrap:wrap;gap:8px}}
 .titlewall img{{width:160px}}
 .cap-noop{{opacity:.4}}
 .shots{{display:flex;gap:4px;margin:.3rem 0}}
 .shots img{{border-radius:2px}}
</style>
""" + sticker_layer() + f"""
<header><div class="wrap">
<h1 id="top">{LOGO_SVG}<span>{h1_text}</span></h1>
<p class="lead"><b>{n_builds}</b> bewertete Builds aus <b>{n_runs}</b> L&auml;ufen.
Jede Runde unter eigener URL; die Bewertung stammt vom Grader, nicht vom Augenschein.</p>
""" + tabs(tab_key, mount) + (sub_tabs("archiv", mount) if tab_key == "archiv" else "") + f"""
<nav>""" + "".join(
    f'<a href="#{a}">{html.escape(t)}<small>{html.escape(n)}</small></a>' for a, t, n in nav
) + """</nav>
</div></header>
<div class="wrap">"""]

    # Podium: die drei besten Zeilen, ganz oben, bevor irgendetwas anderes kommt.
    # Gewertet wird pro Zeile ihr BESTER Build, sortiert erst nach Gate, dann nach
    # Checkliste. Gate zuerst, weil es die robustere Zahl ist: an identischen Builds
    # gemessen schwankt die Checkliste um +-3 Punkte, das Gate blieb stabil. Sonst
    # stuende eine Zeile mit 20/24 und kaputter Eingabe vor einer mit 20/24, die laeuft.
    def best_of(items, run_dir=None):
        # Nachbewertung beruecksichtigen: die aufgezeichnete Zahl bleibt der Beleg (und
        # steht so in den Tabellen), aber fuer die RANGFOLGE zaehlt der Wert des aktuellen
        # Graders. Sonst stuende eine Zeile zu tief, weil ihr bester Build noch mit dem
        # kaputten Tastatur-Test gemessen wurde -- heute folgenlos, weil keine Zeile ihren
        # Bestwert in einer nachbewerteten Runde hat, aber nicht auf Dauer.
        rg = {}
        if run_dir:
            for g in sorted(glob.glob(os.path.join(run_dir, "regrade-*.json"))):
                try:
                    rg = {x["label"]: x for x in json.load(open(g)).get("rounds", [])}
                except Exception:
                    pass
        cands = []
        for b, rel in items:
            res = b["res"] or {}
            cap = res.get("capability") or {}
            if res.get("ladder_rung") is None or not cap.get("total"):
                continue
            gate = res["ladder_rung"]
            if b["label"] in rg and rg[b["label"]].get("rung") is not None:
                gate = rg[b["label"]]["rung"]
            cands.append((gate, cap.get("passed", 0), b, rel))
        # Schluessel explizit: bei Gleichstand in Gate UND Checkliste wuerde `max` sonst
        # die Build-Dictionaries vergleichen und mit TypeError abbrechen.
        return max(cands, key=lambda c: (c[0], c[1])) if cands else None

    # v3: places are decided by v3.podium_places(), not a plain sort -- two rows within
    # the tie rule's margin (span-aware; see podium_places()) share a place instead of one
    # arbitrarily outranking the other. `cap` is `gate*100 + checklist passed`, so gate
    # stays the dominant, robust criterion (a gate-6 row can never tie a gate-5 one) and
    # the tie rule only ever acts *within* one gate, on the checklist's own 0-24 spread --
    # matching what `span` (a checklist re-grading range) actually measures. A v2 build has
    # no `span`, i.e. width 0, so two v2 rows only ever share a place on an exact
    # (gate, checklist) match -- the one case the old bare sort left to arbitrary order
    # anyway, never a place a v2 page used to show as clearly distinct.
    candidates = []
    for (game, stamp), by_row in contests.items():
        for row, items in by_row.items():
            hit = best_of(items, os.path.join(repo, "benchmarks", game, "runs",
                                              f"{stamp}--{row}"))
            if hit:
                gate, cap, b, rel = hit
                candidates.append({"cap": gate * 100 + cap, "gate": gate, "raw_cap": cap,
                                   "span": (b["res"] or {}).get("span"),
                                   "b": b, "rel": rel, "row": row, "items": items})
    for (game, run), items in singles.items():
        hit = best_of(items, os.path.join(repo, "benchmarks", game, "runs", run))
        if hit:
            gate, cap, b, rel = hit
            candidates.append({"cap": gate * 100 + cap, "gate": gate, "raw_cap": cap,
                               "span": (b["res"] or {}).get("span"),
                               "b": b, "rel": rel, "row": run, "items": items})
    podium = [p for p in v3.podium_places(candidates) if p["place"] <= 3]

    if live:
        parts.append('<section id="live"><h2>L&auml;uft gerade</h2>'
                     '<p class="setup">aktive Zeilen mit ihrer bisher letzten Runde</p>'
                     '<div class="chips">')
        for stamp, row, items in sorted(live, key=lambda x: x[1]):
            rounds = sorted([b for b, _ in items if b["kind"] == "round"],
                            key=lambda b: b["rnd"])
            last = next((x for x in items if x[0] is (rounds[-1] if rounds else None)), None)
            if not last:
                continue
            parts.append(chip(last[0], last[1], label=f'{row} · {last[0]["label"]}'))
        parts.append('</div><a class="top" href="#' + anchor(live[0][0]) +
                     '">&darr; alle Runden im Abschnitt</a></section>')

    if podium:
        parts.append('<section id="podium"><h2>Spitzenreiter</h2>'
                     '<p class="setup">bester Build je Zeile &mdash; sortiert nach Gate, '
                     'dann Checkliste; bei enger Spanne geteilter Platz</p><div class="podium">')
        for p in podium:
            i, gate, cap, b, rel, row, items, shared = (
                p["place"], p["gate"], p["raw_cap"], p["b"], p["rel"], p["row"], p["items"], p["shared"])
            su = next((x.get("setup") for x, _ in items if x.get("setup")), "")
            peak = (b["res"] or {}).get("peak_state") or {}
            end = " &middot; Sieg" if peak.get("ended") == "victory" else ""
            st, _ = strip_and_badge(items)
            play = html.escape(rel + "?autoplay=0")
            watch = html.escape(rel + "?autoplay=1")
            shared_html = ' <span class="shared">geteilt</span>' if shared else ""
            parts.append(
                f'<div class="pod p{i}"><div class="rank">{i}</div>'
                f'<div class="who">{html.escape(row)}</div>'
                f'<div class="score">{gate}/{gate_max(b["res"] or {})} &middot; {cap}/24{end}{shared_html}</div>'
                f'{st}'
                f'<div class="cfg">{html.escape(su)}</div>'
                f'<div class="acts"><a href="{play}">spielen</a>'
                f'<a href="{watch}">&#9654; autoplay</a>'
                + vote_button(VOTE_REPO, row) +
                f'<span class="rd">{html.escape(b["label"])}</span></div></div>')
        parts.append('</div></section>')

    if not rows_subset:
        parts.append('<p class="empty">Noch keine Builds gesammelt. Runden erscheinen hier, '
                     'sobald sie bewertet sind: <code>bin/arena-serve --refresh</code>.</p>')

    last_game = None
    for game, stamp in contest_keys:
        if game != last_game:
            parts.append(f'<h2>{html.escape(game)}</h2>')
            last_game = game
        by_row = contests[(game, stamp)]
        parts.append(f'<section id="{anchor(stamp)}">'
                     f'<h2>{html.escape(game)} &mdash; Wettlauf {html.escape(stamp)}</h2>')
        for m in sorted(by_row):
            items = sorted(by_row[m], key=lambda x: (x[0]["kind"] != "round", x[0]["rnd"]))
            su = next((b.get("setup") for b, _ in items if b.get("setup")), "")
            st, bg = strip_and_badge(items)
            parts.append(f'<div class="rowhead" id="{anchor(m)}">'
                         f'<a class="rowlab" href="#{anchor(m)}">{html.escape(m)}</a>'
                         f'{st}{bg}{vote_button(VOTE_REPO, m)}</div>')
            if su:
                parts.append(f'<p class="setup">{html.escape(su)}</p>')
            parts.append('<div class="chips">'
                         + "".join(chip(b, rel) for b, rel in items)
                         + "</div>")
        parts.append('<a class="top" href="#top">&uarr; nach oben</a></section>')

    last_game = None
    for game, run in single_keys:
        if game != last_game:
            parts.append(f'<h2>{html.escape(game)}</h2>')
            last_game = game
        items = sorted(singles[(game, run)], key=lambda x: (x[0]["kind"] != "round", x[0]["rnd"]))
        su = next((b.get("setup") for b, _ in items if b.get("setup")), "")
        st, bg = strip_and_badge(items)
        parts.append(f'<section id="{anchor(run)}">'
                     f'<h2>{html.escape(game)} &mdash; {html.escape(run)}</h2>'
                     + (f'<div class="rowhead">{st}{bg}</div>' if st or bg else ""))
        if su:
            parts.append(f'<p class="setup">{html.escape(su)}</p>')
        parts.append('<div class="chips">'
                     + "".join(chip(b, rel) for b, rel in items)
                     + '</div><a class="top" href="#top">&uarr; nach oben</a></section>')

    reach = "" if PUBLIC else " Nur im Tailnet erreichbar."
    parts.append("""<p class="legend">
<b>Gate 0&ndash;6</b> &mdash; Spielbarkeit, jede Runde neu verdient.
<b>0&ndash;24</b> &mdash; Checkliste, was vom Spiel beobachtet wurde.
Ein Klick auf die Kachel spielt den Build (<code>?autoplay=0</code>, du steuerst),
&#9654; startet ihn so, wie der Grader ihn f&auml;hrt (<code>?autoplay=1</code>)."""
                 + reach + "</p></div>" + vote_script(VOTE_REPO))

    with open(os.path.join(root, out_name), "w") as fh:
        fh.write("\n".join(parts))
    return parts


if __name__ == "__main__":
    main()

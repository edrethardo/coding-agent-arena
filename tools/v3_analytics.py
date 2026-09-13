"""Die vier v3-Bausteine des Analytics-Reiters. Reine Funktionen: Daten rein, HTML raus.
Wallzeit erscheint nur im Zeitstrahl -- als Ausdehnung, nie als Zahl im Text (REVIEW B2).

Der Container der Ereignisleiste heisst hier ".evstrip", nicht ".strip" -- Letzteres ist
in collect_builds.py bereits die Klasse fuer den Runden-Verlaufsstreifen (Gate-Quadrate,
inline-flex). Gleicher Name, gleiches Stylesheet, andere Form haette den alten Streifen
kaputt gemacht (v2 muss unveraendert bleiben)."""
import html, json, os, time

def _esc(s): return html.escape(str(s))

def throughput_rows(runs):
    out = []
    for game, run, prov, rounds in runs:
        for r in rounds:
            p = r.get("proxy") or {}
            if not p or "calls" not in p: continue
            out.append({"run": run, "row": prov.get("row", run.split("--")[-1]), "round": r.get("round"),
                        "out_tok_s": p.get("out_tok_s_median"), "ttft_s": p.get("ttft_median_s"),
                        "prompt_max": p.get("prompt_tokens_max"), "cache": p.get("cache_read_share"),
                        "reasoning": p.get("reasoning_share_mean"), "tool_share": p.get("tool_result_share_mean"),
                        "calls": p.get("calls"), "inflight": p.get("inflight_max") or 0,
                        "warn": (p.get("inflight_max") or 0) >= 4})
    return out

def throughput_table(rows):
    h = ['<table class="tp"><tr><th>Zeile</th><th>R</th><th>tok/s</th><th>TTFT s</th><th>Prompt max</th><th>Cache</th><th>Reasoning</th><th>Tool-Anteil</th><th>Aufrufe</th><th>gleichzeitig</th></tr>']
    for r in rows:
        cls = ' class="warn"' if r["warn"] else ""
        f = lambda v, fmt: (fmt % v) if isinstance(v, (int, float)) else "–"
        h.append(f'<tr{cls}><td>{_esc(r["row"])}</td><td>{r["round"]}</td><td>{f(r["out_tok_s"], "%.1f")}</td><td>{f(r["ttft_s"], "%.2f")}</td>'
                 f'<td>{f(r["prompt_max"], "%d")}</td><td>{"–" if r["cache"] is None else "%.0f%%" % (100*r["cache"])}</td>'
                 f'<td>{"–" if r["reasoning"] is None else "%.0f%%" % (100*r["reasoning"])}</td><td>{"–" if r["tool_share"] is None else "%.0f%%" % (100*r["tool_share"])}</td>'
                 f'<td>{r["calls"]}</td><td>{r["inflight"]}</td></tr>')
    h.append("</table><p class=note>gelb: ≥ 4 gleichzeitig offene Streams beim Aufruf — tok/s und TTFT stehen dann unter Last.</p>")
    return "\n".join(h)

def event_strip(run_dir, rnd, res):
    """Zeitachse der Runde mit Markern: Kompaktierung (Prompt faellt ≥ 30 %), HTTP ≥ 400,
    erstes Schreiben einer Datei, Ende. Spec 6 verlangt beide: den Schreib-Marker (er trennt
    "der Agent hat nachgedacht und dann gebaut" von "der Agent hat nie etwas angefasst") und
    die QUELLE jedes Markers im Tooltip -- Proxy oder Runner, denn die beiden messen mit
    verschiedenen Uhren und verschiedener Aufloesung."""
    p = os.path.join(run_dir, "proxy-requests.jsonl")
    recs = []
    if os.path.exists(p):
        for line in open(p):
            try:
                o = json.loads(line)
                if o.get("round") == rnd: recs.append(o)
            except Exception: pass
    if not recs: return '<div class="evstrip empty">keine Proxy-Daten</div>'
    t0 = recs[0]["t"]; span = max(1.0, float(res.get("wall_s") or (recs[-1]["t"] - t0) or 1))
    ev = []; prev = None
    for o in recs:
        x = 100.0 * (o["t"] - t0) / span
        pt = o.get("prompt_tokens") or 0
        if prev and pt and prev >= 4000 and pt < 0.7 * prev:
            ev.append(f'<span class="ev compaction" style="left:{x:.1f}%" title="Proxy: Kompaktierung, {prev} → {pt} Prompt-Token zwischen zwei Aufrufen"></span>')
        if pt: prev = pt
        if (o.get("status") or 200) >= 400:
            ev.append(f'<span class="ev http" style="left:{x:.1f}%" title="Proxy: HTTP {o["status"]} vom Upstream, 1:1 durchgereicht">{o["status"]}</span>')
    # Erstes Schreiben einer Datei (Spec 6). Quelle ist der Runner, nicht der Proxy: er tastet
    # die mtime des Arbeitsverzeichnisses alle 30 s ab, die Marke ist also auf ~30 s genau --
    # das steht im Tooltip, damit die Position nicht feiner gelesen wird, als sie ist.
    fw = res.get("first_write_t")
    if fw:
        try:
            started = time.mktime(time.strptime(res["started"], "%Y-%m-%dT%H:%M:%S"))
            fx = max(0.0, min(100.0, 100.0 * (float(fw) - started) / span))
            ev.append(f'<span class="ev write" style="left:{fx:.1f}%" '
                      f'title="Runner: mtime — erste Datei im Arbeitsverzeichnis nach {int(float(fw) - started)} s '
                      f'(alle 30 s abgetastet)"></span>')
        except Exception:
            pass
    end = res.get("exit_meaning", "")
    if end not in ("ok", ""): ev.append(f'<span class="ev exit" style="left:100%" title="Runner: exit — {_esc(end)}">{_esc(end)}</span>')
    return f'<div class="evstrip" title="{len(recs)} Aufrufe">{"".join(ev)}</div>'

def gantt(runs):
    """Alle Zeilen ueber die Wanduhr; darunter die Zahl gleichzeitig laufender Runden."""
    bars = []
    for game, run, prov, rounds in runs:
        row = prov.get("row", run.split("--")[-1])
        for r in rounds:
            try: s = time.mktime(time.strptime(r["started"], "%Y-%m-%dT%H:%M:%S"))
            except Exception: continue
            bars.append((row, s, s + float(r.get("wall_s") or 0), r.get("round")))
    if not bars: return "<p>keine Runden</p>"
    t0 = min(b[1] for b in bars); t1 = max(b[2] for b in bars); span = max(1.0, t1 - t0)
    rows = sorted({b[0] for b in bars}); W, H = 900, 18 * len(rows) + 60
    svg = [f'<svg viewBox="0 0 {W} {H}" class="gantt">']
    for i, row in enumerate(rows):
        y = 10 + i * 18
        svg.append(f'<text x="2" y="{y+12}" font-size="10">{_esc(row)}</text>')
        for b in bars:
            if b[0] != row: continue
            x = 150 + (b[1] - t0) / span * (W - 160); w = max(2, (b[2] - b[1]) / span * (W - 160))
            svg.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="14" fill="#e0202f" opacity=".8"><title>{_esc(row)} R{b[3]}</title></rect>')
    edges = sorted([(b[1], 1) for b in bars] + [(b[2], -1) for b in bars]); cur = mx = 0; pts = []
    for t, d in edges:
        cur += d; mx = max(mx, cur); pts.append((150 + (t - t0) / span * (W - 160), H - 10 - cur * 4))
    svg.append('<polyline fill="none" stroke="#3a6fd8" stroke-width="1.5" points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + '"/>')
    svg.append(f'<text x="150" y="{H-2}" font-size="10">gleichzeitig laufende Runden, max {mx}</text></svg>')
    return "\n".join(svg)

def title_wall(builds):
    """One card per ROW (game, run), from that row's LAST round that has a title.png --
    not one card per round. A row graded across 7 rounds would otherwise repeat its own
    title screen seven times on the wall."""
    best = {}
    for b in builds:
        if b.get("kind") != "round":
            continue
        shots = (b.get("res") or {}).get("screenshots") or []
        t = next((s for s in shots if s.endswith("title.png")), None)
        if not t:
            continue
        key = (b["game"], b["run"])
        if key not in best or b["rnd"] > best[key][0]:
            best[key] = (b["rnd"], b, t)
    cards = []
    for (game, run), (rnd, b, t) in sorted(best.items()):
        cards.append(f'<a class="wall" href="{_esc(b["href"])}"><img src="{_esc(b["run_rel"] + "/" + t)}" alt="Titelbild {_esc(run)}"><span>{_esc(b.get("setup") or run)}</span></a>')
    return '<div class="titlewall">' + "".join(cards) + "</div>" if cards else ""

def _span_width(span):
    return (span[1] - span[0]) if span and len(span) == 2 else 0

def podium_places(candidates):
    """Podium tie rule (spec S6): a place is only awarded over the next candidate when
    the capability gap exceeds the LARGER of the two rows' spans (span = [min, max] over
    a round's re-gradings; a v2 row carries no span, treated as width 0 -- so with only
    v2 candidates this reduces to "tied only on an exact capability match", i.e. v2's
    prior, span-less behaviour is unchanged).

    `candidates` is a list of dicts, each with at least a "cap" (capability score) and
    optionally a "span" ([min, max]); any other keys are carried through untouched.
    Returns the same dicts, sorted best-first, each with "place" (1-based, Olympic-style:
    a tied pair both take the better place and the next place skips ahead) and "shared"
    (True iff at least one neighbour in its tie group)."""
    ranked = sorted(candidates, key=lambda c: c.get("cap", 0), reverse=True)
    out = []
    place = 1
    i = 0
    n = len(ranked)
    while i < n:
        group = [ranked[i]]
        j = i + 1
        while j < n:
            gap = group[-1].get("cap", 0) - ranked[j].get("cap", 0)
            if gap <= max(_span_width(group[-1].get("span")), _span_width(ranked[j].get("span"))):
                group.append(ranked[j])
                j += 1
            else:
                break
        shared = len(group) > 1
        for c in group:
            out.append(dict(c, place=place, shared=shared))
        place += len(group)
        i = j
    return out

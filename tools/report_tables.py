"""Erzeugt die Ergebnistabellen für BERICHT.md aus den Laufverzeichnissen.

Liest ausschließlich die aufgezeichneten Ergebnisse plus, falls vorhanden, die
Nachbewertung (`regrade-*.json`). Wo beide existieren, wird der nachbewertete Wert gezeigt
und der aufgezeichnete in Klammern dahinter — die ursprüngliche Zahl ist der Beleg dafür,
was damals gemessen wurde, und verschwindet nicht.
"""
import glob
import json
import os
import sys

BASE = "benchmarks/antifa_survivors_v2/runs"
STAMP = sys.argv[1] if len(sys.argv) > 1 else "H-20260909T003403"


def regrades(run_dir):
    """label -> rung aus der jüngsten Nachbewertung dieses Laufs."""
    files = sorted(glob.glob(os.path.join(run_dir, "regrade-*.json")))
    if not files:
        return {}
    try:
        d = json.load(open(files[-1]))
    except Exception:
        return {}
    return {r["label"]: r for r in d.get("rounds", [])}


def load():
    out = []
    for d in sorted(glob.glob(f"{BASE}/{STAMP}--*")):
        row = os.path.basename(d).split("--", 1)[1]
        rg = regrades(d)
        for f in glob.glob(f"{d}/results_*.json"):
            data = json.load(open(f))
            out.append((row, data.get("rounds", []), rg, d))
    return out


def main():
    rows = load()

    print("### Verlauf: Gate / Checkliste je Runde\n")
    print("| Zeile | R1 | R2 | R3 | R4 | R5 | R6 | R7 |")
    print("|---|---|---|---|---|---|---|---|")
    for row, rounds, rg, _ in sorted(rows):
        cells = {}
        for r in rounds:
            c = r.get("capability") or {}
            g = r["ladder_rung"]
            key = f"r{r['round']}"
            if key in rg and rg[key].get("rung") != g:
                cells[r["round"]] = f"**{rg[key]['rung']}**/6 · {c.get('passed')} *(aufgez. {g})*"
            else:
                cells[r["round"]] = f"{g}/6 · {c.get('passed')}"
        print(f"| `{row}` | " + " | ".join(cells.get(i, "—") for i in range(1, 8)) + " |")

    print("\n### Endstand\n")
    print("| Zeile | Harness | Modell | Gate | Checkliste | Bosse | Stage | Ende | Build |")
    print("|---|---|---|---|---|---|---|---|---|")
    final = []
    for row, rounds, rg, d in rows:
        if not rounds:
            continue
        last = rounds[-1]
        c = last.get("capability") or {}
        p = last.get("peak_state") or {}
        prov = {}
        pf = os.path.join(d, "provenance.json")
        if os.path.exists(pf):
            prov = json.load(open(pf))
        b = os.path.join(d, f"r{last['round']}", "index.html")
        final.append((c.get("passed", 0), row, prov.get("harness", "?"),
                      prov.get("model_declared", "?"), last["ladder_rung"], c.get("passed"),
                      f"{p.get('bosses_spawned')}/{p.get('bosses_defeated')}",
                      p.get("stage"), p.get("ended"),
                      f"{os.path.getsize(b):,}" if os.path.exists(b) else "—"))
    for _, row, h, m, g, cap, boss, stage, end, size in sorted(final, reverse=True):
        print(f"| `{row}` | {h} | `{m}` | {g}/6 | {cap}/24 | {boss} | {stage} | "
              f"{end if end else '—'} | {size} B |")

    print("\n### Modell konstant, Harness variabel\n")
    pairs = {"claude-opus-5": ("hermes-claude-opus-5", "cc-opus-5"),
             "claude-sonnet-5": ("hermes-claude-sonnet-5", "cc-sonnet-5")}
    by_row = {r[0]: r[1] for r in rows}
    print("| Modell | Harness | Gate | Checkliste | Runden |")
    print("|---|---|---|---|---|")
    for model, (a, b) in pairs.items():
        for row in (a, b):
            rs = by_row.get(row) or []
            if not rs:
                continue
            last = rs[-1]
            c = last.get("capability") or {}
            h = "Hermes" if row.startswith("hermes") else "Claude Code"
            print(f"| `{model}` | {h} | {last['ladder_rung']}/6 | {c.get('passed')}/24 | {len(rs)}/7 |")


if __name__ == "__main__":
    main()

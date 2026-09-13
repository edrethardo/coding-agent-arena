# Antifa Survivors — Coding-Agent-Testbench

Sieben Runden, eine Sitzung, ein Artefakt. Coding-Agenten bauen ein Vampire-Survivors-artiges Browserspiel. Bewertet wird **das Spiel**, nicht der Chat.

Öffentliche Ergebnisse (spielbare Builds, Bericht, Publikumswertung):
**https://edrethardo.github.io/antifa-survivors-arena/**

Dieses Repository ist die **Testbench**: Spezifikation, Rundenaufträge, Headless-Grader, Runner, Trace-Proxy. Die Kampagnen-Rohdaten (`runs/`) gehören nicht hierher.

---

## Was gemessen wird

Zwei Zahlen, immer aus derselben Bewertung:

| Maß | Bereich | Bedeutung |
|---|---|---|
| Spielbarkeits-Gate | 0–5 | Lädt, Titelbild, Start per Taste **und** Touch, reagiert, läuft flüssig |
| Fähigkeits-Checkliste | 0–24 | Inhalt der Spezifikation: Gegner, Waffen, Passive, Evolutionen, Bosse, Musik, Ende |

Jeder Build wird **dreimal** gespielt. Gewertet wird der Median je Check; die Spanne `[min, max]` wird mitgeliefert.

Der Agent sieht die Checkliste nie. Er bekommt die Spezifikation (`base.txt`) und den jeweiligen Rundenauftrag. Die Datei bleibt auf der Platte und wird **nicht** in den Prompt geklebt. `window.__state` ist Sensor, kein Beweis: Kills ohne Bildänderung, Sieg ohne Boss, Stage ohne Bosssieg werden verworfen.

## Wie ein Lauf aussieht

1. Eine Agentensitzung je Zeile (Modell × Harness), sieben Folgenachrichten.
2. Runde 1: Spezifikation + State-Vertrag + erster Auftrag. Runden 2–7: nur der neue Auftrag.
3. Nach jeder Runde: Grader (Firefox + geckodriver), drei Wiederholungen, Screenshots.
4. Zwischen Agent und Modell hängt ein Trace-Proxy: Prompt-/Cache-Token, Zeit bis zum ersten Token, Anteil der Werkzeugergebnisse.

Typische Zeilen: Claude (Opus / Sonnet / Fable / Haiku) × Hermes und Claude Code; Qwen3.8-27B in mehreren Hermes-Konfigurationen; GPT-5.6 Sol/Terra/Luna über Hermes.

## Voraussetzungen

- Python 3.10+
- Firefox und `geckodriver` (der Grader erwartet `/snap/bin/geckodriver`, überschreibbar)
- Für einen **vollen Lauf**: der jeweilige Agent (Hermes oder Claude Code) und ein Modell-Endpunkt
- Für **nur bewerten**: ein fertiges Spielverzeichnis mit `index.html`

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Ein Build bewerten (ohne Agent)

```bash
.venv/bin/python benchmarks/antifa_survivors_v3/grader.py path/zum/spiel --json
```

Nützliche Schalter: `--seconds N` (sonst zufällig 180–300), `--gate-seconds 45`, `--seed S`, `--shots DIR`.

Selbsttest der Fixtures:

```bash
.venv/bin/python benchmarks/antifa_survivors_v3/grader_selftest.py
python - <<'PY'
import tests.test_v3_bench as t
for n in sorted(dir(t)):
    if n.startswith("test_"):
        getattr(t, n)()
        print("ok", n)
PY
```

## Eine Zeile fahren

```bash
bin/arena-run --check --game antifa_survivors_v3 --row v3-hermes-claude-sonnet-5
# wenn die Gates grün sind:
bin/arena-run --game antifa_survivors_v3 --row v3-hermes-claude-sonnet-5 --label V3
```

`--check` rührt GPU und Modell nicht an. Der Runner schreibt nach `benchmarks/antifa_survivors_v3/runs/<id>/`.

Umgebungsvariablen (Auszug):

| Variable | Zweck |
|---|---|
| `QWEN_BASE_URL` | OpenAI-kompatibler Endpunkt, Standard `http://127.0.0.1:8000/v1` |
| `QWEN_MODEL` | Modellname am lokalen Server |
| `ARENA_DRY_RUN=1` | druckt den Startbefehl, startet nicht |

Zugangsdaten gehören in die Agent-Installation, nicht in dieses Repo.

## Seite bauen

```bash
bin/arena-publish ./dist-public
```

Erzeugt die öffentliche HTML-Übersicht aus vorhandenen Runs. Vor dem Schreiben läuft ein Privacy-Gate (keine privaten Hosts, keine Home-Pfade).

## Was hier nicht steht

- Keine Checklisten-Schwellen im Prompt (sonst wird auf die Metrik hin gebaut).
- Kein Claim, ein Modell sei „besser“ als ein anderes, wenn die Harness wechselt.
- Keine Roh-Traces und keine Kampagnen-`runs/` in diesem Tree — die öffentlichen Builds liegen im Pages-Repo.

Ausführliche Begründung der v3-Entscheidungen: [`docs/superpowers/specs/2026-09-09-antifa-survivors-v3-design.md`](docs/superpowers/specs/2026-09-09-antifa-survivors-v3-design.md).

## Lizenz

MIT. Die von den Agenten gebauten Spiele in der Arena bleiben Artefakte der jeweiligen Läufe.

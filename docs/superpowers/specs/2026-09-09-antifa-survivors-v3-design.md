# Antifa Survivors v3 — Design

Stand 2026-09-09. Ersetzt `benchmarks/antifa_survivors_v3/ENTWURF.md` als verbindliche Fassung;
der Entwurf bleibt als Vorgeschichte liegen.

## 0. Ziel

Ein Lauf, der zwei Fragen beantwortet, die v2 nicht beantworten konnte:

1. **Was taugen die Agenten?** Vier Claude-Modelle, jedes in zwei Harnesses (Hermes, Claude Code),
   gleichzeitig, gegen dieselbe Aufgabe und denselben Grader.
2. **Welche Hermes-Konfiguration trägt für Qwen?** Ein Modell, eine Harness, vier Configs, die sich
   von einer Referenz um je einen Knopf unterscheiden.

v2 hat stattdessen gemessen, ob Modell+Harness+Serving eine wachsende Datei im Kontext halten, mit
einem Grader, der eine ganze Modellfamilie aus einem Blindfleck heraus abgewertet hat
(`benchmarks/antifa_survivors_v2/REVIEW.md`). Jede Lehre daraus steht unten mit ihrer Antwort.

## 1. Lehren aus v2 und ihre Antwort in v3

| Review-Fund | v3 |
|---|---|
| A1 Grader schickt nur `TouchEvent`; Pointer-Event-Builds gelten als tot | Eingabe über echte WebDriver-Actions (`KeyActions`, `PointerInput(kind="touch")`); Fixtures in drei Bauformen |
| A2 Gate auf identischen Bytes nicht stabil | drei Bewertungen je Build, Median je Check, Spanne wird berichtet |
| A3 No-op-Runden als Arbeit erzählt | Build-Hash über alle Dateien je Runde; No-op wird in Ergebnis und Seite markiert |
| A4 `fps60` misst den rAF des Graders | fps = Canvas-Prüfsummenwechsel je Sekunde; fps-Sprosse aus dem Gate gestrichen |
| A5 eine Zeile 17 h später allein gelaufen | alle Remote-Zeilen starten in einem `arena-contest`-Aufruf; Start-/Endzeit je Zeile auf der Seite |
| A6 Grader mitten im Lauf editiert, kein Hash | `grader_sha256` in `bench.json` festgeschrieben; Preflight bricht bei Abweichung ab |
| A7 Zellen mischen zwei Bewertungen | Gate und Checkliste kommen immer aus derselben Bewertungs-Dreiergruppe |
| B2 Parallelitätszahlen erfunden | `wall_s` wird aufgezeichnet, nirgends als Leistung gezeigt; Proxy zählt gleichzeitige Streams |
| B8 „Denkanteil" war gen/prompt | Reasoning-Anteil aus den `thinking`-Blöcken bzw. `<think>`-Segmenten des Streams |
| E1 20 von 24 Checks sind Selbstauskunft | Selbstauskunft bleibt Sensor, aber plausibilitätsgeprüft (§3.4); vier neue unabhängige Signale |
| E2 Metrik, Schwellen und Fenster offengelegt | Prompt nennt keine Checks, keine Schwellen, keine Dauer; Fenster zufällig 180–300 s |
| §1 des Entwurfs: Vordatei im Prompt | Datei bleibt auf der Platte, Prompt rundenkonstant |

## 2. Spiel und Prompt

### 2.1 Spezifikation (`base.txt`)

Wie v2 (Vampire-Survivors-artig, iPhone Safari Portrait und Desktop, statische Dateien, keine
Bauwerkzeuge, kein Netz, keine externen Assets, Daten-getriebene Waffen/Gegner/Passives), mit drei
Ergänzungen:

- **Titelbild** (ab Runde 1). Canvas-gezeichnet. Bildsprache: drei nach links unten zeigende Pfeile,
  zwei Fahnen, Schwarz-Rot, Sticker-Ästhetik; Spielname. Startet mit einer Taste **und** mit Touch.
  `?autoplay=1` überspringt es nach ≤ 1 s, es muss aber zuvor gezeichnet werden.
- **Punkrock-Soundtrack** (ab Runde 2, verfeinert in Runde 7). WebAudio, prozedural: Oszillator-Bass,
  Rausch-Drums, Verzerrung über `WaveShaperNode`, 170–200 bpm. Läuft auf dem Titelbild und im Spiel,
  reagiert auf Boss, Level-up und Tod, ist stummschaltbar (Taste M und Touch-Knopf). Startet erst
  nach einer Nutzereingabe (Autoplay-Policy der Browser).
- **Mehrere Dateien erlaubt.** `index.html` ist Pflichteinstieg; daneben beliebige `*.js`/`*.css`
  über relative Pfade. Gesamtgröße ohne Grenze, aber keine Bilddateien, keine Audiodateien.

### 2.2 Rundenaufträge (`rounds.json`)

Die sieben Aufträge aus v2, mit zwei Änderungen: Runde 1 verlangt das Titelbild, Runde 2 den
Soundtrack, Runde 7 („make it feel good") verfeinert Musik und Titelbild statt Sound neu einzuführen.

### 2.3 State-Vertrag

`window.__state` wie v2 (Felder: `mode` (`title`|`running`|`ended`), `player{x,y,hp}`, `kills`,
`level`, `weapons[]`, `passives[]`, `evolutions[]`, `enemies`, `bystanders`, `nipsters{hidden,revealed}`,
`bosses{spawned,defeated}`, `stage`, `ended`, `alive`, `autoplay`, `audio{playing,muted}`), jeden
Frame aktualisiert. Das ist die Schnittstelle und steht deshalb im Prompt.

### 2.4 Was der Prompt sagt und was nicht

**Runde 1** enthält: Spezifikation, State-Vertrag, den ersten Rundenauftrag, und über die Bewertung
genau dies: *„An automated checker plays the game for a few minutes with keyboard and touch and
reads `__state`. It scores how much of the specification is demonstrably running. Reporting state
your game does not have is the one way to fail outright."*

**Runde 2–7** enthalten nur noch den neuen Auftrag — *„ROUND n of 7 — …"* — als Folgenachricht in
derselben Sitzung (§2.5), so wie ein Mensch nachlegt. Keine Spec-Wiederholung, keine Vordatei.

Nicht enthalten, in keiner Runde: die Checks, die Gate-Leiter, Schwellen (4/8/12 Waffen …), die
Sitzungsdauer, die Vordatei im Prompt.

### 2.5 Eine Sitzung je Zeile („wie echtes Prompten")

Jede Zeile ist **eine** Agentensitzung über alle sieben Runden. Der Agent erinnert sich an Spec
und eigene Arbeit; was er davon behält, entscheidet die Harness (Kompaktierung, Pruning,
Werkzeug-Hygiene) — und genau das macht die Konfigurationsachse (§4.2) messbar: über sieben
frische Kurzsitzungen griffe eine Kompressionsschwelle womöglich nie.

Mechanik je Harness:
- Hermes: `--continue arena-<run_id> --create-if-missing` in jeder Runde (Runde 1 legt die
  benannte Sitzung an, Runde 2–7 setzen sie fort); Sitzungen liegen in der `HERMES_HOME` der Zeile.
- Claude Code: Runde 1 `--session-id <uuid>`, Runde 2–7 `--resume <uuid>`; die UUID vergibt der
  Runner deterministisch aus `run_id`.
- Wrapper-Vertrag, zusätzlich zu `<wrapper> <dir> < task.txt`: Umgebung `ARENA_SESSION_NAME`,
  `ARENA_SESSION_ID` (UUID), `ARENA_SESSION_CONTINUE` (`0` in Runde 1 oder nach Fallback, sonst `1`).
  Exit **9** = Sitzung nicht fortsetzbar.

**Fallback-Regel.** Meldet der Wrapper Exit 9, wiederholt der Runner die Runde einmal als frische
Sitzung mit dem vollen Runde-1-Prompt plus dem aktuellen Auftrag, unter neuem Sitzungsnamen, und
schreibt `session_fallback: true` ins Rundenergebnis. Das ist ein Messwert, kein Reparaturversuch:
eine Zeile, die ihre Sitzung verliert, hat das erlebt, was ein Nutzer erlebt hätte.

`task_chars` ist damit in Runde 1 ~15 kB und in Runde 2–7 < 2 kB.

## 3. Grader (`benchmarks/antifa_survivors_v3/grader.py`)

Ein Skript, ~400 Zeilen, von oben nach unten lesbar; Firefox headless über Selenium 4.47 mit
geckodriver `/snap/bin/geckodriver`. Bedient den Build über einen lokalen HTTP-Server auf einem
freien Port (Multi-Datei-Builds; `file://` blockt Modul-Imports).

### 3.1 Ablauf einer Bewertung

1. **Titelbild.** Laden ohne Parameter, 2 s, Screenshot `title.png`, Canvas-Farbzählung, JS-Fehler.
   `__state.mode == "title"`.
2. **Start per Taste.** `KeyActions` Enter; `mode` muss `running` werden. Neu laden.
3. **Start per Touch.** `PointerInput(kind="touch")` Tap aufs Canvas; `mode` muss `running` werden.
4. **Eingabe.** Bei `?autoplay=0`: Taste D 1,2 s halten, dann A 1,2 s halten — `player.x` muss
   entsprechend wandern, gegen die Drift eines eingabefreien Fensters gemessen. Touch-Drag 24 Schritte
   über das Canvas — `player` muss sich ≥ 40 px bewegen. Echte Actions erzeugen Touch-, Pointer- und
   Mouse-Events wie ein Gerät.
5. **Sitzung.** `?autoplay=1`, Dauer `window_s` aus `[180, 300]` gezogen (Seed aus
   `run_id + round + repeat`, in der Provenance). Alle 2 s: `__state`-Sample + Canvas-Prüfsumme.
6. **Audio-Abzug** bei 30 s: `AudioContext.state`, Knotentypen im Graph (Monkeypatch von
   `createOscillator`, `createWaveShaper`, `createGain` vor dem Laden zählt Erzeugungen).
   `music_playing` = AudioContext erzeugt **und** ≥ 1 Oszillator **und** ≥ 1 WaveShaper **und**
   `OscillatorNode.start()` aufgerufen **und** `resume()` aufgerufen (oder Zustand nicht `closed`).
   Nicht `state == running`: der Grader läuft auf einer Box ohne Audio-Backend, dort bleibt jeder
   Context `suspended` — ein Check, der daran hinge, wäre tot wie v2's `fps60`. Der Zustand wird
   trotzdem aufgezeichnet.
7. **Screenshots** bei 30 s, 120 s und am Ende → `r<n>/grade-<k>/{title,t30,t120,end}.png`.
8. **fps** = Bilder je Sekunde, in denen sich das Canvas tatsächlich geändert hat: die injizierte
   Probe zählt auf dem `requestAnimationFrame` der Seite nur solche Ticks, in denen eine kleine
   Pixelstichprobe (`getImageData` auf einem Ausschnitt) gegenüber dem vorigen Tick abweicht.
   Median über die Sitzung. Nicht der rAF-Zähler allein (A4) und nicht Zeichenaufrufe allein
   (ein Build, der 60× je Sekunde dasselbe Bild malt, hat keine 60 fps).

### 3.2 Gate 0–5

| Sprosse | Bedingung |
|---|---|
| 1 | lädt ohne unbehandelten JS-Fehler |
| 2 | Canvas ≥ 3 Farben und Prüfsumme ändert sich |
| 3 | Taste **und** Touch bewegen den Spieler (3.1 Schritt 4) |
| 4 | Kills steigen unter Autoplay, plausibel (§3.4) |
| 5 | nach 45 s Autoplay `alive`, mit Kills oder Level, ohne Laufzeitfehler |

### 3.3 Checkliste 0–24

`state_contract`, `title_screen`, `manual_control`, `keyboard_input`, `touch_input`, `kills`,
`survives_window`, `music_playing`, `enemies`, `level_up`, `weapons_4`, `weapons_8`, `weapons_12`,
`passive_1`, `passives_6`, `evolution_1`, `evolutions_5`, `bystanders`, `nipster_hidden`,
`nipster_revealed`, `boss_spawned`, `boss_defeated`, `stage_2`, `victory`.

Gegenüber v2: `autoplay_entry` → `title_screen`, `fps60` → `music_playing`. fps wird weiter
aufgezeichnet und gezeigt, aber nicht gewertet.

### 3.4 Plausibilitätsregeln

Jede Regel, die greift, wird im Ergebnis unter `rejected[]` mit Sample-Index protokolliert.

- `kills`/`level` zählen nur, wenn sich zwischen zwei Samples die Canvas-Prüfsumme geändert hat.
- `victory` zählt nur, wenn `bosses.defeated ≥ 1` in einem **früheren** Sample stand.
- `stage_2` zählt nur nach `bosses.defeated ≥ 1`.
- `weapons_*`, `passive_*`, `evolution_*` zählen nur nach mindestens einem Level-Anstieg.
- `survives_window` verlangt `alive` **und** Canvas-Änderung im letzten Sample.
- **Alle** Spitzenwerte aus `__state` (auch `enemies`, `bystanders`, `stage`, `nipsters`, `bosses`)
  werden nur aus Samples übernommen, in denen sich das Canvas geändert hat; ein Anstieg in einem
  unveränderten Sample wird je Feld einmal protokolliert. Selbstauskunft ist Sensor, kein Beweis.

### 3.5 Drei Bewertungen

`grade()` ruft den Grader dreimal mit verschiedenen Seeds. Ergebnis je Runde:
`grades[3]` (roh), `gate` = Median der drei Gates, `checks` = je Check Median (0/1 → Mehrheit),
`capability` = Summe der Median-Checks, `span` = min/max der Capability. Gate und Checkliste
stammen immer aus derselben Dreiergruppe.

### 3.6 Ausgabe

`--json` liefert `{gate, capability, checks{}, span, fps, window_s, seed, rejected[], audio{},
screenshots[], notes[]}`. Ohne `--json` dieselben Werte lesbar.

### 3.7 Hash-Historie

`grader_sha256` wurde am 2026-09-10 gegen ~10:00 neu eingefroren (Fix-Runde 3, live reproduziert:
ein Build, der nach Sitzungsende seinen JS-Thread mit `while(true){}` blockiert, ließ den Grader
~20 Minuten in einem einzelnen WebDriver-Aufruf hängen — Selenium hat ohne Konfiguration keinen
Kommando-Timeout — und manchmal Firefox-Prozesse zurück). Der Fix: ein Timeout auf jedem
WebDriver-Kommando (`COMMAND_TIMEOUT_S = 90`, gesetzt über `client_config.timeout` am bereits
gebauten `command_executor`, da `webdriver.Firefox()` in Selenium 4.47 kein `client_config=`
entgegennimmt); jeder Schritt nach der Sampling-Schleife (Endscreenshot, letzter Audio-/
Fehler-Abzug) ist best-effort und bricht einzeln mit einer Notiz ab, statt den ganzen Lauf zu
verlieren; und ein robuster Teardown — `d.quit()` läuft in einem Daemon-Thread mit eigenem,
kürzerem Timeout (`TEARDOWN_TIMEOUT_S`), weil `Service._terminate_process()` sonst zusätzlich
bis zu 60 s hartkodiert auf den geckodriver-Prozess wartet, unabhängig von `client_config`; egal
ob `d.quit()` durchläuft, folgt danach IMMER ein PID-basiertes SIGTERM/SIGKILL über den ganzen
Prozessbaum (geckodriver + alle Firefox-Nachfahren, via `/proc/<pid>/task/*/children`), nie
`pkill -f` nach Muster. Geändert ist ausschließlich diese Robustheit rund um die Sitzung —
`evaluate()`, die Plausibilitätsregeln, das Gate, `CHECKS`, die fps-Probe und die Eingabe-Pfade
(§3.1 Schritte 2–4) sind byte-identisch zum vorherigen Hash. Wertungen von vor und nach diesem
Freeze sind vergleichbar.

2026-09-11: robustness against scalar state fields; scoring unchanged.

## 4. Kampagne

### 4.1 Zeilen

| Zeile | Harness | Modell | Backend | Wrapper / Umgebung |
|---|---|---|---|---|
| `hermes-claude-{opus-5,fable-5-1,sonnet-5,haiku-4-5}` | Hermes | Claude | Anthropic via Proxy (Port je Zeile, 8020–8027) | `bin/hermes-task-model`, `HERMES_HOME=~/.hermes-arena/v3-claude-<modell>` |
| `cc-{opus-5,fable-5-1,sonnet-5,haiku-4-5}` | Claude Code | Claude | Anthropic via Proxy (Port je Zeile, 8020–8027) | `bin/claude-code-task`, `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` |
| `hermes-qwen-rec` | Hermes | Qwen3.8-27B | vLLM via Proxy (Port je Zeile, 8010–8013) | `HERMES_HOME=~/.hermes-arena/v3-rec` |
| `hermes-qwen-stock` | Hermes | Qwen3.8-27B | vLLM via Proxy (Port je Zeile, 8010–8013) | `HERMES_HOME=~/.hermes-arena/v3-stock` |
| `hermes-qwen-rec-low` | Hermes | Qwen3.8-27B | vLLM via Proxy (Port je Zeile, 8010–8013) | `HERMES_HOME=~/.hermes-arena/v3-rec-low` |
| `hermes-qwen-rec-131k` | Hermes | Qwen3.8-27B | vLLM via Proxy (Port je Zeile, 8010–8013), Serving auf 131072 | `HERMES_HOME=~/.hermes-arena/v3-rec-131k` |

Sieben Runden je Zeile, ein Lauf je Zeile: 84 Runden. Die acht Remote-Zeilen starten in **einem**
`arena-contest`-Aufruf; parallel dazu läuft jeweils **eine** Qwen-Kampagne (vLLM-Zähler sind je
Server, nicht je Zeile). Reihenfolge lokal: `rec`, `stock`, `rec-low`, dann Serving-Wechsel,
`rec-131k`.

### 4.2 Qwen-Configs

`v3-rec` ist die Referenz. Jede andere Variante ist `v3-rec` plus genau ein Unterschied; die
Provenance speichert den YAML-Diff gegen `v3-rec`. **Ausnahme: `v3-rec2`** (siehe unten) ist ein
korrigiertes Bündel, kein Ein-Knopf-Unterschied — die Provenance der `rec2`-Zeilen diffed deshalb
gegen `v3-rec2`, nicht gegen `v3-rec` (`run_game_bench.hermes_diff`, Präfix-Zuordnung
`HERMES_DIFF_REFERENCE`). `v3-rec3` ist wieder ein Ein-Knopf-Schritt, aber ab `v3-rec2-low`, nicht
ab `v3-rec`: seine Referenz in derselben Zuordnung ist `v3-rec2-low`. `v3-rec4` ist ein
Zwei-Knopf-Schritt ab `v3-rec2` (nicht `v3-rec2-low`: `reasoning_effort` bleibt `medium`);
Referenz `v3-rec2`. `v3-rec3-131k` ist ein Zwei-Knopf-Schritt ab `v3-rec3`; Referenz `v3-rec3`
(der `HERMES_DIFF_REFERENCE`-Eintrag für `v3-rec3-131k` steht VOR dem allgemeineren
`v3-rec3`-Präfix in der Liste, sonst würde der Zeilenname selbst — er beginnt mit
`v3-hermes-qwen-rec3` — auf die falsche, kürzere Referenz matchen). `hermes_diff` filtert
außerdem `_config_version` aus jedem Vergleich heraus: das ist Hermes' eigene
Schema-Migrationsnummer, keine Zeilen-Einstellung, und bewegt sich mit der
Arbeitsinstallation unabhängig davon, wann eine Variante zuletzt neu erzeugt wurde —
unbereinigt hätte sie in JEDEM künftigen Variantenvergleich als falscher Unterschied
aufgetaucht, sobald zwei Varianten zu verschiedenen Zeitpunkten neu erzeugt wurden.

| Variante | Unterschied zu `v3-rec` |
|---|---|
| `v3-rec` | `model.context_length: 65536`, `model.max_tokens: 16384`, `compression.threshold_tokens: 40000` (= Fenster − max_tokens − ~9k Reserve für die Kompaktierungsanfrage selbst; mit 49000 starb die Integrationsrunde S-044433 in Runde 2: Prompt 53.567 + max_tokens 16.384 > 65.536 → HTTP 400, „max compression attempts (3) reached“), `agent.reasoning_effort: medium`, `model.base_url: http://127.0.0.1:<port>/v1`; Rest wie `~/.hermes/config.yaml`. **Zeile geschlossen** (Aaron, 2026-09-10): mit `threshold_tokens: 40000` bei unveränderten `compression.protect_last_n: 20` (≈27k Token geschützter Schwanz, nie schrumpfbar) und `compression.target_ratio: 0.2` (≈13k-Token-Zusammenfassung) landete jede Kompaktierung wieder genau auf der Schwelle, von der sie ausgelöst wurde — jeder zweite Aufruf wurde selbst eine Kompaktierung, und Runde 1 lief in den Timeout statt fertigzuwerden. Ersetzt durch `v3-rec2` |
| `v3-stock` | ohne `max_tokens`, ohne `threshold_tokens` (= das Bündel der Empfehlung fehlt; das ist der eine Unterschied) |
| `v3-rec-low` | `agent.reasoning_effort: low` |
| `v3-rec-131k` | `model.context_length: 131072`; `threshold_tokens` bleibt 40000 (absolute Grenze, das ist der Punkt). **Optional, zuletzt**: braucht den Serving-Wechsel durch GPU Master (§10); entfällt ohne ihn ersatzlos, der Bericht sagt das, und die übrige Kampagne wartet nicht darauf |
| `v3-rec2` (Port 8014) | Korrigiertes Bündel statt Ein-Knopf-Variante, Referenz für sich selbst: `compression.threshold_tokens: 49000`, `compression.protect_last_n: 6`, `compression.target_ratio: 0.1`; `model.max_tokens: 16384`, `model.context_length: 65536`, `agent.reasoning_effort: medium` bleiben wie `v3-rec`. Behebt `v3-rec`s Timeout, indem die Knöpfe geändert werden, die bestimmen, WIEVIEL eine Kompaktierung tatsächlich zurückgewinnt (Schwanzgröße, Zusammenfassungsgröße), statt nur, WANN sie auslöst: ein kleinerer, nicht an die Obergrenze gepinnter Schwanz (6 statt 20 geschützte Nachrichten) und eine knappere Zusammenfassung (0.1 statt 0.2) geben jeder Kompaktierung Raum, tatsächlich unter die Schwelle zu fallen, statt sofort wieder dagegen zu laufen; der höhere `threshold_tokens` (49000) ist unproblematisch, sobald Kompaktierung wieder richtig zurückgewinnt |
| `v3-rec2-low` (Port 8015) | `agent.reasoning_effort: low`, sonst wie `v3-rec2` |
| `v3-rec3` (Port 8016) | `v3-rec2-low` plus genau ein Unterschied (Referenz `v3-rec2-low`, nicht `v3-rec`): ein `custom_providers`-Eintrag, der zum eigenen `base_url` passt und `extra_body: {chat_template_kwargs: {reasoning_effort: "none"}}` trägt. Grund (an GPU Master gegen vLLM 0.27.1 verifiziert): vLLMs `merge_kwargs` lässt ein Top-Level-`reasoning_effort` je Anfrage gewinnen — der Hauptmodellaufruf schickt `reasoning_effort: low` auf oberster Ebene und behält es. Hermes' Kompaktierungsaufruf (die Zusammenfassung, die bei jeder Schwellenüberschreitung läuft) trägt aber KEIN Top-Level-`reasoning_effort` — sein eigener Effort-Wert geht als `extra_body.reasoning` hinaus, ein Feld, das vLLM für die Template-Auswahl nicht liest — sodass für genau diesen Aufruf nur das `custom_providers`-`chat_template_kwargs` entscheidet, und das steht heute auf dem Template-Standard `xhigh`: ~11.000 Token Denken für jede einzelne Kompaktierungs-Zusammenfassung. Passung per `base_url` (exakter String-Vergleich nach Entfernen eines schließenden `/`, `agent/agent_init.py` `_normalized_custom_base_url`/`_custom_provider_extra_body_for_agent`) und `model.provider: custom` (kein `provider_key`/`name`-Filter nötig, da kein `custom:<name>`-Suffix verwendet wird) |
| `v3-rec4` (Port 8017) | `v3-rec2` (nicht `v3-rec2-low` — `agent.reasoning_effort` bleibt `medium`) plus zwei Ergänzungen (Referenz `v3-rec2`). Gemessen auf den Live-Läufen: Tool-Ergebnisse (die wachsende Spieldatei, jede Runde neu gelesen) sind 27–54 % jedes Qwen-Prompts (Median 40 % in `rec2`s Runde 2, einzelne Ergebnisse bis 71.000 Zeichen), und Hermes' eigene CLI-Agent-Konstruktion (`hermes_cli/cli_agent_setup_mixin.py`) reicht `model.max_tokens` nie als `max_tokens=` an `AIAgent.__init__` durch — bestätigt durch Quellenlektüre, nicht angenommen: `agent.max_tokens` bleibt für die ganze Sitzung `None`, `agent/transports/chat_completions.py`s `_apply_max_tokens` setzt deshalb nie einen Wire-Level-Cap, und `model.max_tokens: 16384` war in JEDER bisherigen Variante (`rec`/`rec2`/`rec3` eingeschlossen) eine tote Einstellung — einzelne Generierungen liefen bis 50.000 Token. (1) `compression.proactive_prune_tokens: 30000`, `compression.proactive_prune_min_result_chars: 4000`, `compression.proactive_prune_min_reclaim_tokens: 4096` — Schlüsselnamen gegen `agent/context_compressor.py` geprüft, keine Umbenennung nötig; `min_result_chars` hat dort eine Untergrenze `_PRUNE_MIN_CHARS=200`, die 4000 nicht berührt; `min_reclaim_tokens: 4096` ist bereits der ererbte Default aus `~/.hermes/config.yaml` und erscheint deshalb NICHT als Diff-Zeile gegen `v3-rec2` (nur als Absicherung gegen einen künftig geänderten Default explizit gesetzt). Schneidet einen großen Tool-Ergebnis-Text deterministisch (ohne LLM-Aufruf) zurecht, BEVOR eine Kompaktierung nötig wird — 30000 liegt bewusst unter `threshold_tokens: 49000`. (2) derselbe `custom_providers`-Zustellweg wie `v3-rec3` (passend per `base_url` + `provider: custom`), aber `extra_body: {max_tokens: 32768}` statt `chat_template_kwargs` — das ist der Cap, der tatsächlich beim Server ankommt, weil er über den OpenAI-SDK-`extra_body`-Mechanismus ins Wire-JSON gemischt wird, statt über das nie durchgereichte `model.max_tokens`. **32768, nicht 16384** (Korrektur vor dem ersten Lauf, aus gemessenen Daten): Aufrufe über 16384 Ausgabe-Token sind nur 2 % aller Aufrufe, aber genau dieser Anteil sind (a) pathologische Denk-Ausreißer (40.000–163.000 Zeichen Denken) und (b) Hermes' EIGENE Kompaktierungs-Zusammenfassungen (18.000–25.000 Ausgabe-Token, ~30.000 Zeichen, `stop: stop`, kein Tool-Aufruf) — ein Cap von 16384 hätte die Zusammenfassung selbst abgeschnitten und genau das „max compression attempts (3) reached"-Sterben reproduziert, das `v3-rec2` verhindern soll; 32768 liegt über den beobachteten Zusammenfassungen und unter den 40.000–50.000 Zeichen der Ausreißer. Ein niedrigerer Cap ist nur in Kombination mit `v3-rec3`s denkfreier Kompaktierung sicher (`chat_template_kwargs.reasoning_effort: "none"`) — `v3-rec4` trägt dieses Knopf nicht, seine Kompaktierung denkt also wie jeder andere Aufruf. Laufzeitbeleg (kein Modellaufruf während der Erstellung — die Karte lief `v3-hermes-qwen-stock`): `proxy-requests.jsonl` zeichnet `req_max_tokens` je Aufruf auf (`tools/trace_proxy.py`); der erste Aufruf dieser Zeile muss `req_max_tokens: 32768` zeigen, und `prompt_tokens` darf zwischen zwei Aufrufen OHNE eine Kompaktierung fallen (das proaktive Pruning greift) |
| `v3-rec3-131k` (Port 8018) | `v3-rec3` plus GENAU zwei Unterschiede, die das größere Fenster erzwingt (Referenz `v3-rec3`). Kurzzeitig am 2026-09-11 zu `v3-rec3-98k` umbenannt, als eine pauschale `kv_cache_max_concurrency >= 2,0`-Schwelle `131072/209132 = 1,60` als FAIL las und die Box bei `131072` nicht mehr Pool servieren konnte (1,24 GiB fehlen, nur 1,5 GiB frei, der Spec-Decode-Puffer braucht den Rest). Noch am selben Tag zurückgedreht, nachdem zwei Dinge tatsächlich GEMESSEN statt angenommen wurden: (1) der Pool ist über Fenster hinweg NICHT konstant — drei von der Box selbst gemessene Punkte: `131072` → Pool `209132` → Konkurrenz `1,60`; `98304` → Pool `180980` → `1,84`; `81920` → Pool `164297` → `2,006` (Fit: Pool ≈ 96524 + 0,859·Fenster) — eine pauschale `2,0`-Schwelle bestrafte diese Zeile für einen Pool, der mit dem Fenster mitwächst, statt irgendetwas Reales über die Zeile zu messen; (2) die `2,0`-Zahl stand stellvertretend für eine Frage, die die eigenen Proxy-Traces direkt beantworten: über alle vier Qwen-Zeilen hinweg war `inflight` bei 2247 von 2248 Modellaufrufen gleich 1 (`rec` 299/299, `rec2` 223/223, `rec3` 740/740, `stock` 985/986) — Hermes ist strikt sequenziell, Kompaktierung überlappt nie mit dem Hauptaufruf, diese Zeile braucht also ein Fenster Pool plus Marge, nicht zwei. `bin/arena-preflight-v3` verlangt jetzt `kv_cache_size_tokens >= 1,25 · context_length` statt der pauschalen `2,0`-Schwelle — bei `131072/209132` sind das `1,60`, klar über der neuen Grenze. Die `2,0`-Zahl war ein Überbleibsel aus der Ära, als acht Zeilen sich einen Endpunkt teilten, und wurde hier fälschlich auf ein einzelnes, sequenzielles Experiment angewendet. (1) `model.context_length: 131072` — Hermes liest `context_length` als OVERRIDE, nicht als Entdeckung: `65536` stehen zu lassen, während der Server `131072` serviert, würde Kompaktierung bei einem 65536-Budget belassen, egal was die Karte jetzt hält — exakt die v2-Falle (der Server wurde am 2026-09-09 auf 131072 verdoppelt, ohne dass es die Harness bemerkte, weil auch hier niemand das Fenster vom Server las). (2) `compression.threshold_tokens: 90000` — gemessene Größen: der Build ist ~109 kB (~30k Token) plus ~15k Token Spec/System, ein Lesen der Spieldatei also ~43k Token, Lesen+Zurückschreiben (das Muster jeder Runde) ~71k — 90000 ist die erste Schwelle, die beides mit Marge hält. `131072 − 90000 = 41072` Token Generierraum — `rec3` schickt kein `max_tokens` (anders als `rec4`), das ist also gemessener Spielraum, kein hartes Budget: Kompaktierungs-Zusammenfassungen unter `rec3`s denkfreier Kompaktierung liegen gemessen bei ~9.800 Token, bequem innerhalb der 41.072. `protect_last_n: 6`, `agent.reasoning_effort: low`, die `chat_template_kwargs`-Kompaktierung (Basis-URL auf Port 8018 nachgeführt) und alles sonst bleiben wie `v3-rec3` |

`~/.hermes/config.yaml` wird nicht angefasst. `v3-claude-<modell>` (vier Verzeichnisse, nur der Port unterscheidet sie) ist `~/.hermes/config.yaml` mit
`model.provider: anthropic`, `model.base_url: http://arena-proxy.anthropic.com.localhost:<port>/anthropic`;
Modell kommt per `-m` vom Wrapper. Der Hostname ist bewusst so gewählt: Hermes behandelt nur URLs, die
`anthropic.com` enthalten, als offiziell (`agent/anthropic_endpoints.py`) — bei jedem anderen Host schickt
es den OAuth-Token als `x-api-key` ohne die OAuth-Beta-Header (→ 401) und fällt danach am Proxy vorbei
auf den Direktweg zurück (Integration 2026-09-10, Lauf S-041856). `*.localhost` löst systemweit ohne
`/etc/hosts`-Eintrag auf `::1` auf, der Proxy hört deshalb auf beiden Loopbacks (127.0.0.1 und ::1); das
`/anthropic`-Suffix ist Hermes' Marker für das Messages-Protokoll und wird vom Proxy vor dem Weiterleiten
entfernt. Mit diesem Namen verhält sich Hermes exakt wie gegen die echte API; der Proxy sieht jeden Aufruf.

### 4.3 Runner (`run_game_bench.py`)

Gesteuert über `benchmarks/antifa_survivors_v3/bench.json`; v2 ohne diese Datei verhält sich
unverändert.

```json
{"paste_prior_file": false, "session_mode": "continue", "grade_repeats": 3, "window_s": [180, 300],
 "grader_sha256": "<beim Einfrieren gesetzt>", "proxy_required": true}
```

- `compose(bench, rnd, workdir, continuing)` hängt bei `paste_prior_file: false` keine Datei an;
  bei `session_mode: "continue"` und `continuing=True` liefert es nur den Rundenauftrag (§2.4).
- Sitzung: Runner setzt `ARENA_SESSION_NAME`, `ARENA_SESSION_ID`, `ARENA_SESSION_CONTINUE`; Exit 9
  löst den Fallback aus §2.5 aus; Ergebnis trägt `session_id`, `session_fallback`.
- `grade()` bewertet `grade_repeats`-mal (§3.5) und legt Screenshots unter `r<n>/grade-<k>/` ab.
- `build_sha256_16` hasht alle Dateien des Arbeitsverzeichnisses (sortiert, Pfad + Inhalt); No-op
  wird als `noop: true` ins Rundenergebnis geschrieben.
- Provenance je Lauf: `grader_sha256`, `bench_sha256` (base.txt + rounds.json + bench.json),
  `proxy_sha256`, `proxy_port`, `hermes_home` + Config-Diff, `serving_stack` (lokal), Startzeit.
- Provenance je Runde: `window_seed[3]`, Proxy-Aggregate (§5.3), `wall_s` (aufgezeichnet, nicht
  gezeigt).
- Neue Zeilen im `WRAPPERS`-Dict bekommen ein Feld `proxy_port`. **Ein Proxy-Prozess je Zeile**:
  der Runner startet ihn beim Kampagnenstart mit `--row`, `--run-id`, `--dialect`, `--upstream`
  und beendet ihn am Ende. Damit ist jeder Span eindeutig einer Zeile zugeordnet, ohne Kopfzeilen,
  die Claude Code nicht setzen kann. Ports: 8010–8013 lokal (Qwen-Varianten), 8020–8027 remote;
  die Zuordnung steht in der Provenance. `env` der Zeile setzt `HERMES_HOME` bzw.
  `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>`; die Hermes-Varianten tragen den Port in ihrer
  `base_url`.

### 4.4 Preflight (`bin/arena-preflight-v3`)

Vor jeder Kampagne, Abbruch bei Verstoß: Sandbox-Selbsttest, `grader_sha256` gegen `bench.json`,
Proxy für die Zeile startbar und Upstream erreichbar (ein `GET /v1/models` lokal bzw. ein
Anthropic-`HEAD`), Serving-Stack-Fingerabdruck, Credential vorhanden (Hermes `auth status`,
Claude Code `claude auth status`), keine andere lokale Zeile aktiv.

**Serving-Stack-Fingerabdruck (lokal):** das erwartete Fenster kommt aus der VARIANTE SELBST
(`model.context_length` in ihrer eigenen `config.yaml`, gelesen über
`WRAPPERS[row][1]['HERMES_HOME']`), nicht aus einem Namens-Suffix — ein `*131k`-Namens-Hack
hört auf etwas zu bedeuten, sobald ein Fenster umbenannt wird (`rec3-131k` hieß am
2026-09-11 kurz `rec3-98k`, noch am selben Tag zurückgedreht — siehe §4.2 und
`bin/hermes-variant`). Der Server muss genau dieses `max_model_len` melden. Kein einzelner
erwarteter Pool-Wert je Fenster, sondern eine Tabelle, gekeyed auf (Fenster, `cache_dtype`) —
dasselbe 131072-Fenster lief bereits über ZWEI Stacks mit unterschiedlichem Pool
(`int8_per_token_head` 136429, KVarN `kvarn_k4v2_g128` 209132 — nicht die 268169 aus KVarNs
eigenem README, die für DESSEN Default-Fenster 245760 gelten, nicht für unseres); ein
einzelner erwarteter Wert je Fenster hätte unbemerkt akzeptiert, welcher Stack gerade lief.
Bekannte Kombinationen (±2 % Toleranz, da sich die exakte Zahl zwischen Stack-Builds etwas
bewegt): `65536`/`bfloat16` → `68605`; `81920`/`kvarn_k4v2_g128` → `164297`;
`98304`/`kvarn_k4v2_g128` → `180980`; `131072`/`int8_per_token_head` → `136429`;
`131072`/`kvarn_k4v2_g128` → `209132`. Eine UNBEKANNTE (Fenster, dtype)-Kombination FAILt mit
der vollständigen bekannten Tabelle in der Meldung, statt stillschweigend durchzulassen.

Der Pool ist selbst bei GLEICHEM `cache_dtype` über Fenster hinweg NICHT konstant — er ist
eine Eigenschaft des allozierten GPU-Speichers und WÄCHST mit dem angeforderten Fenster: die
drei obigen `kvarn_k4v2_g128`-Punkte fitten grob `Pool ≈ 96524 + 0,859·Fenster` (ein Fit, kein
Gesetz — ein neues Fenster braucht einen eigenen gemessenen Tabelleneintrag, keinen
angenommenen/interpolierten). Für JEDES Fenster > 65536 zusätzlich Pflicht: der Pool muss
mindestens **1,25 volle Fenster** halten (`kv_cache_size_tokens >= 1,25 · context_length`) —
das ersetzt die frühere pauschale `kv_cache_max_concurrency >= 2,0`-Schwelle. Die `2,0`-Zahl
stammte aus der Ära, als acht Zeilen sich einen Endpunkt teilten und echte parallele Anfragen
zwei Fenster Marge brauchten; sie wurde auf dieses einzelne, sequenzielle Experiment fälschlich
angewendet. Die eigenen Proxy-Traces beantworten die Frage direkt, für die `2,0` stellvertretend
stand: über alle vier Qwen-Zeilen war `inflight` bei 2247 von 2248 Modellaufrufen gleich 1
(`rec` 299/299, `rec2` 223/223, `rec3` 740/740, `stock` 985/986) — Hermes ist strikt
sequenziell, Kompaktierung überlappt nie mit dem Hauptaufruf, eine Zeile braucht also ein
Fenster Pool plus Marge, nicht zwei; `1,25` ist diese Marge. Bei `131072/209132`
(`v3-rec3-131k`) ergibt das `1,60`, deutlich über der Schwelle. Die `ok`-Zeile druckt Fenster,
`max_model_len`, `dtype`, `pool`, das Verhältnis (`ratio`) und die daraus abgeleitete
Konkurrenz (`concurrency`, aus `pool`/Fenster berechnet, nicht aus vLLMs eigenem gemeldeten
Feld übernommen).

## 5. Messpfad

### 5.1 Proxy (`tools/trace_proxy.py`)

Ein Programm, zwei Dialekte, gewählt per `--dialect openai|anthropic`.

- Kopfzeilen: alle durchreichen außer `Host`, `Connection`, `Content-Length`; `Accept-Encoding`
  entfernen (Antwort bleibt unkomprimiert und parsebar).
- Streaming ohne Puffer (vorhanden): jeder Chunk geht raus, sobald er da ist.
- Fehler 1:1: Status und Body des Upstreams werden weitergereicht; keine Wiederholung, kein Fallback.
- Anthropic-Dialekt: Anfrage `POST /v1/messages` (auch mit `?beta=true`, auch unter Präfix
  `/anthropic`), Antwort-SSE: `message_start.usage{input_tokens, cache_read_input_tokens,
  cache_creation_input_tokens}`, `message_delta.usage.output_tokens`, `content_block_start.type`
  ∈ {`thinking`, `text`, `tool_use`} mit Zeichenzählung je Typ.
- OpenAI-Dialekt (vorhanden): `usage` aus dem letzten Chunk, `<think>`-Segmente als Reasoning.

### 5.2 Span je Modellaufruf

Phoenix-Projekt `arena-v3-proxy`, OpenInference-LLM-Span mit: `row`, `run_id`, `round`, `model`,
`llm.token_count.{prompt,completion}`, `cache_read_tokens`, `cache_write_tokens`, `req.messages`,
`req.tool_result_chars`, `req.tool_result_share`, `resp.reasoning_chars`, `resp.text_chars`,
`resp.tool_use_count`, `resp.reasoning_share` (= reasoning / (reasoning + text)), `ttft_s`,
`stream_s`, `out_tok_s` (= completion / (stream_s − ttft_s)), `inflight` (gleichzeitig offene
Streams dieses Proxys beim Start), `status`.

### 5.3 Aggregate je Runde (in `results`)

`calls`, `prompt_tokens_sum`, `prompt_tokens_max`, `completion_tokens_sum`, `cache_read_share`,
`tool_result_share_mean`, `reasoning_share_mean`, `ttft_median_s`, `out_tok_s_median`,
`inflight_max`. Der Proxy schreibt sie je Runde nach `r<n>/proxy.json`; der Runner nimmt sie ins
Ergebnis.

### 5.4 Weiter wie v2

vLLM-Zähler je Runde (nur lokale Zeilen, mit Reset-Erkennung); Runden-Span des Runners in
`antifa-survivors-<row>`; beide über `run_id` verknüpft.

## 6. Seite (`tools/collect_builds.py`, `bin/arena-serve`)

v3 als eigene Serie oberhalb von v2; v2 bleibt erreichbar mit Verweis auf `REVIEW.md`.

**Je Zeile:** Verlaufstabelle Gate/Checkliste mit **Spanne** als Hochzahl, No-op-Runden grau,
Screenshot-Streifen (`title`, `t120`) je Runde, Audio-Abzug als Symbol, Start-/Endzeit der Zeile.
Podium nur, wenn der Abstand der Capability-Mediane größer ist als die größere der beiden Spannen;
sonst „geteilt".

**Reiter „Analytics"**, je Zeile und Runde:

- Durchsatz: `out_tok_s_median`, `ttft_median_s`, `prompt_tokens_max`, `cache_read_share`,
  `reasoning_share_mean`, `tool_result_share_mean`, `calls`; daneben `inflight_max` als Warnfarbe
  (≥ 4 gelb) — die Zahl ist nur so gut wie die Last, unter der sie entstand.
- **Ereignisstreifen je Runde:** eine Zeitachse von Rundenstart bis -ende mit Markierungen für
  Kompaktierung (Proxy: Prompt-Token fallen zwischen zwei Aufrufen um ≥ 30 %), 429/5xx (Proxy:
  `status`), Timeout und Harness-Fehler (Runner: `exit_meaning`), erstes Schreiben einer Datei
  (Runner: mtime-Sprung im Arbeitsverzeichnis, alle 10 s abgetastet). Quelle je Marker steht im
  Tooltip. Das ist die Geschichte hinter einer schlechten Zahl.
- **Zeitstrahl:** Gantt aller Zeilen über die Wanduhr, eine Spur je Zeile, Runden als Balken;
  darunter die Kurve der gleichzeitig laufenden Runden. Ehrliche Parallelität statt einer
  Zahl im Text (A5/B2-Lehre).
- **Titelbild-Wand:** `title.png` jeder Zeile aus der letzten Runde nebeneinander, Klick öffnet
  den Build. Kein Score — zum Anschauen.

## 7. Tests

### 7.1 Grader-Fixtures (`benchmarks/antifa_survivors_v3/fixtures/`)

| Fixture | Bauform | Erwartung |
|---|---|---|
| `fx-touch` | Joystick über `touchstart/move` | Sprosse 3 ✓ |
| `fx-pointer` | Joystick über `pointerdown/move` | Sprosse 3 ✓ |
| `fx-mouse` | Joystick über `mousedown/move` | Sprosse 3 ✓ |
| `fx-frozen` | Canvas statisch, `kills` steigt | `kills` verworfen, Sprosse 4 ✗ |
| `fx-fake-victory` | `ended: "victory"`, nie ein Boss | `victory` verworfen |
| `fx-slow` | rAF läuft, Canvas malt 10 Hz | fps ≈ 10 |
| `fx-multi` | `index.html` + `game.js` + `style.css` | lädt; Hash umfasst drei Dateien |
| `fx-silent` | kein AudioContext | `music_playing` ✗ |
| `fx-no-title` | startet sofort | `title_screen` ✗ |
| `fx-good` | alles | Gate 5, drei Bewertungen, Median + Spanne |

`grader_selftest.py` läuft alle durch; Pflicht vor dem Einfrieren von `grader_sha256`.

### 7.2 Proxy (`tools/test_trace_proxy.py`)

Fake-Upstream für beide Dialekte. Prüft: Kopfzeilen kommen an, `Accept-Encoding` nicht; Usage und
Blocktypen korrekt; 429 mit Body 1:1 zurück; erster Chunk < 50 ms nach dem Upstream; `inflight`
zählt zwei parallele Streams; `proxy.json` je Runde stimmt mit den Spans überein.

### 7.3 Runner

`compose()` mit `paste_prior_file: false` und `session_mode: "continue"`: Runde 1 trägt die Spec, Runde 2–7 nur den Auftrag (< 2 kB), nie die Vordatei. Wrapper-Stubs belegen `--continue`/`--create-if-missing` bzw. `--session-id`/`--resume` und Exit 9. `grade()` mit
`grade_repeats: 3`: drei Rohergebnisse, Median, Spanne. Provenance enthält alle Felder aus §4.3.
`build_sha256_16` ändert sich bei einer neuen Datei ohne Änderung an `index.html`.

### 7.4 Preflight

Geänderter Grader → Abbruch; Proxy-Port belegt oder Upstream nicht erreichbar → Exit 4;
Serving-Fingerabdruck falsch → Abbruch.

### 7.5 Integration (Wegwerf-Bench, je ZWEI Runden, im Sandkasten — Runde 2 beweist die Fortsetzung)

1. `cc-haiku-4-5` durch den Proxy.
2. `hermes-claude-haiku-4-5` durch den Proxy — prüft die eine ungeprüfte Annahme: Hermes benutzt
   die `base_url` der Variante auch unter `--provider anthropic`. Fallback: `ANTHROPIC_BASE_URL`
   in der Umgebung (Hermes' `auth.py` listet sie).
3. `hermes-qwen-rec`, sobald die Karte frei ist.

Bedingung für den Start der Kampagne: alle drei Zeilen hinterlassen je zwei Runden mit Proxy-Spans,
Grader-Ergebnis mit Screenshots und `proxy.json`, und Runde 2 hat `session_fallback: false` mit
`task_chars < 2000`.

## 8. Reihenfolge der Umsetzung

1. Spec-Review durch Aaron (dieses Dokument).
2. Implementierungsplan (writing-plans).
3. Grader + Fixtures → `grader_sha256` einfrieren.
4. Proxy-Anthropic-Dialekt + Tests.
5. Runner-Änderungen + `bench.json` + `WRAPPERS`-Zeilen + Hermes-Varianten.
6. Preflight, Integration (§7.5).
7. `arena-contest` mit acht Remote-Zeilen + `arena-run hermes-qwen-rec` gleichzeitig; danach
   `stock`, `rec-low`; Serving-Wechsel mit GPU Master; `rec-131k`.
8. Seite, dann Bericht `benchmarks/antifa_survivors_v3/BERICHT.md` — mit REVIEW-Lehren als eigenem
   Abschnitt und ohne Podium, wo die Spanne es nicht hergibt.

Getrennt davon, vor der Veröffentlichung von v2: Korrekturen an `antifa_survivors_v2/BERICHT.md`
nach `REVIEW.md` (Kopfzeile, A1, A3, A4, A5, A7, B2) — eigener Auftrag.

## 9. Nicht-Ziele

Kein Modell-Juror im Score; Titelbild und Musik werden gesammelt, nicht benotet. Keine
Wiederholungsläufe je Zeile (n=1 je Zeile bleibt; die Spanne der drei Bewertungen ersetzt das nicht
und wird auch nicht so dargestellt). Keine pi-/dsh-Zeilen in v3. Keine Veröffentlichung außerhalb
des Tailnets.

## 10. Risiken

- Serving-Befund (Zettel-Session, 2026-09-10 00:xx): `kv_cache_max_concurrency` ist bei 65536/bfloat16
  (68.605 Token) mit 1,05 genauso knapp wie bei 131072/int8 (136.429 Token, 1,04) — die Karte fasst in
  beiden Fassungen knapp eine Sequenz maximaler Länge. Für v3 heißt das: `threshold_tokens: 49000`
  hält jede Anfrage unter ~61 % des 65536-Fensters (40.000 + 16.384 Ausgabe = 56.384 < 68.605 KV-Pool); mehr als eine
  gleichzeitige Anfrage derselben Hermes-Sitzung (Delegation) verdrängt trotzdem. Der Proxy zeichnet
  `inflight` und vLLM `num_preemptions_total` je Runde auf — steigt letzteres bei `v3-stock`
  (ohne absolute Grenze), ist das ein Ergebnis, kein Messfehler.
- Eine Harness verliert die Sitzung (Kompaktierungsabbruch, korrupte Session-Datei) → Fallback nach
  §2.5, sichtbar als `session_fallback`; die Zeile läuft weiter, die Stelle steht im Ergebnis.

- Hermes ignoriert die Varianten-`base_url` unter `--provider anthropic` → §7.5 Schritt 2 vor dem
  Rest; Fallback benannt.
- Anthropic lehnt Anfragen über einen Proxy ab (z. B. wegen `anthropic-dangerous-direct-browser-access`)
  → Kopfzeilen werden vollständig durchgereicht; Test in §7.5 Schritt 1.
- Acht parallele Claude-Zeilen und das Abo: Rate-Limits kommen als 429 durch den Proxy 1:1 an und
  landen als `status` im Span — sichtbar, nicht versteckt. Kein Retry im Proxy; die Harness
  entscheidet.
- `rec-131k` braucht den Serving-Wechsel durch GPU Master; ohne ihn entfällt die Variante und der
  Bericht sagt das.
- Zufälliges Fenster macht `survives_window` zwischen Bewertungen ungleich schwer; deshalb Median
  aus drei Bewertungen und die Seeds in der Provenance.

## Integration 2026-09-10

- cc via proxy: ok -- v3-cc-haiku-4-5, run S-20260910T034233 (nach einem invaliden Vorlauf S-20260910T032802, Plugin-Hijack in Runde 1, siehe NOTE.md dort): beide Runden exit_meaning ok, gate 1/1, proxy.calls 4/6 (alle 200), Runde 2 session_continued true, cache_read_share 0.937 in (0,1); Phoenix-Projekt arena-v3-proxy bestaetigt ueber proxy.log
- hermes-claude via proxy: ok -- v3-hermes-claude-haiku-4-5, run S-20260910T055431 (nach drei invaliden Vorlaeufen: S-20260910T035146 404 wegen /anthropic-Pfad-Bug in trace_proxy, dann S-20260910T040109 und S-20260910T041856 mit korrektem Pfad aber 401 -- Hermes umging den Proxy fuer die eigentliche Generierung trotz ANTHROPIC_BASE_URL-Fallback; alle drei NOTE.md). Fix ohne /etc/hosts: trace_proxy hoert zusaetzlich auf ::1 (8e95ff8), die claude-Varianten zeigen auf http://arena-proxy.anthropic.com.localhost:<port>/anthropic (56b6db9), ein Hostname, den Hermes als offiziell behandelt (OAuth-Header bleiben) und der ohne Hosts-Eintrag auf ::1 aufloest. Danach beide Runden exit_meaning ok, gate 5/5, proxy.calls 18/34 (alle 200), Runde 2 session_continued true, cache_read_share 0.928/0.977 (Weg: config base_url | ANTHROPIC_BASE_URL)
- qwen-rec via proxy: ok -- v3-hermes-qwen-rec, run S-20260910T044433 (GPU-Lock frei, kein Warten noetig; pi/dsh-Kontextfenster-Konfigurationen zuvor von 131072 auf 65536 resynchronisiert, siehe §7.5-Praeflight). Runde 1 exit_meaning ok, gate 4, 56 echte proxy.calls, model_metrics ohne metrics_error. Runde 2 exit_meaning harness-failure -- Kontext erschoepft (prompt_tokens_max 53567 + max_tokens 16384 > 65536-Fenster), ein echter Befund: die empfohlene Kampagnen-Schwelle wird auf 40000 gesenkt; kein Absturz, keine Wiederholung noetig

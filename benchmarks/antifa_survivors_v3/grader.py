# benchmarks/antifa_survivors_v3/grader.py
"""Headless grading for Antifa Survivors v3.

Was gegenueber v2 anders ist, und warum (REVIEW.md):
  * Eingabe kommt aus echten WebDriver-Actions (Tastatur gehalten, Touch-Pointer gezogen).
    v2 schickte synthetische TouchEvents und wertete jeden Pointer-Event-Build als tot (A1).
    Faellt die Touch-Action im Browser aus, feuert der Fallback Touch-, Pointer- UND
    Mouse-Events -- nie nur eine Bauform.
  * fps = wie oft sich das Canvas je Sekunde aendert, nicht der rAF-Zaehler des Graders (A4).
  * Sitzungsdauer zufaellig 180-300 s aus --seed; das Fenster steht nicht im Prompt (E2).
  * Selbstauskunft (__state) ist Sensor, kein Beweis: Kills ohne Bildaenderung, Sieg ohne
    Boss, Stage ohne Bosssieg werden verworfen und protokolliert (E1).
  * Die Probe (Fehler-Listener, Audio-Graph-Zaehler) wird vom Dateiserver in den <head>
    injiziert und laeuft damit VOR den Skripten der Seite -- ein Monkeypatch nach dem Laden
    sieht keinen Knoten, der beim Laden entstand.

    python grader.py <dir> [--seconds N] [--gate-seconds 45] [--seed S] [--shots DIR] [--json]
"""
import argparse, base64, functools, hashlib, http.server, json, os, random, signal, socketserver, sys, threading, time
from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

DRIVER = "/snap/bin/geckodriver"

# Round 3: a build that pegs its JS thread after the grading session ends left the grader
# blocked forever inside a single WebDriver HTTP call (confirmed live: ~5 min session + ~20
# min before Firefox was given up on). Bound every command instead -- see build_driver().
COMMAND_TIMEOUT_S = 90
# d.quit() gets its OWN, much shorter bound, joined via a daemon thread rather than trusted
# to client_config alone (see score()): measured directly (task-3-report.md's round-3 fix
# report) that a healthy quit() takes well under 1s, while quit() on a genuinely stuck content
# process can run past 80s even with client_config.timeout reduced, because
# Service._terminate_process() waits up to a SEPARATE, hardcoded 60s
# (selenium/webdriver/common/service.py) for the geckodriver OS process to exit, entirely
# outside client_config's reach. 10s leaves an order of magnitude of headroom over the
# healthy case while keeping the round-3 fix inside its "window + ~120s" total-time bound.
TEARDOWN_TIMEOUT_S = 10

PROBE_JS = r"""
window.__errs = [];
window.addEventListener('error', e => window.__errs.push(String(e.message)));
window.addEventListener('unhandledrejection', e => window.__errs.push('reject: ' + e.reason));
window.__audio = { contexts: 0, nodes: {}, ctx: null, resumed: 0 };
(function () {
  const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return;
  ['createOscillator', 'createWaveShaper', 'createGain', 'createBufferSource',
   'createBiquadFilter', 'createDynamicsCompressor'].forEach(n => {
    const o = AC.prototype[n]; if (!o) return;
    AC.prototype[n] = function () { window.__audio.nodes[n] = (window.__audio.nodes[n] || 0) + 1; return o.apply(this, arguments); };
  });
  // Build evidence, not playback evidence: on the grading box there is no audio backend at
  // all, so ctx.state never leaves 'suspended' no matter what the build does -- confirmed by
  // hand (real WebDriver clicks, resume() called synchronously inside a genuine click
  // handler, several cubeb/ALSA backend overrides, all tried, none moved ctx.state). Judging
  // "does the build play music" on ctx.state would be dead by construction here, the mirror
  // image of v2's vacuous rAF-tick fps60. So music_playing is decided on what the BUILD did
  // (created a context, wired an oscillator through a shaper, started the oscillator, and
  // attempted a resume -- or never got closed), not on whether the browser's speakers ever
  // actually turned on. `state` is still recorded in audio{} for the record.
  const resumeOrig = AC.prototype.resume;
  if (resumeOrig) {
    AC.prototype.resume = function () { window.__audio.resumed++; return resumeOrig.apply(this, arguments); };
  }
  const P = new Proxy(AC, { construct(t, a) { const c = new t(...a); window.__audio.contexts++; window.__audio.ctx = c; return c; } });
  window.AudioContext = P; if (window.webkitAudioContext) window.webkitAudioContext = P;
  const ON = window.OscillatorNode;
  const startOrig = ON && ON.prototype.start;
  if (startOrig) {
    ON.prototype.start = function () {
      window.__audio.nodes.oscillatorStart = (window.__audio.nodes.oscillatorStart || 0) + 1;
      return startOrig.apply(this, arguments);
    };
  }
})();
window.__canvasFrames = 0;
window.__canvasFramesAlive = 0;
(function () {
  // fps = how often the canvas VISIBLY changes per second, sampled on our own
  // requestAnimationFrame loop. Two earlier versions of this were both wrong: a
  // synchronous busy-loop that repeatedly calls getImageData() blocks the single JS thread
  // for its whole duration, so the page's own rAF callback -- the thing that would actually
  // redraw the canvas -- never runs during the measurement and the read is always zero
  // changes; counting draw-call invocations (fillRect/clearRect/...) instead counts a build
  // that redraws the identical frame every tick as "60 fps" even though nothing on screen
  // moved. Sampling a few small FIXED boxes (tried first: 8x8 at nine grid points -- corners,
  // edge midpoints, center) was measured to fail on our own fx-good: its autoplay motion is
  // a slow sine/cosine drift, so the player and enemies spend most of a several-second
  // window away from every one of those tiny fixed spots, and __canvasFrames plateaus at
  // whatever the mode-transition redraws happened to catch, then never moves again even
  // while the picture keeps changing everywhere else. Measured directly: nine 8x8 boxes on
  // fx-good read the same checksum for 6+ straight seconds while __state.player moved from
  // x=262 to x=649 and back. A single small region simply cannot be trusted to be where the
  // motion is. So this reads the WHOLE canvas every tick instead, at the same sparse stride
  // SIG already uses elsewhere (every 97th pixel) -- cheap enough to keep rAF ticking at a
  // measured ~60/s in practice on a 500x800 canvas with no dropped frames -- and only counts
  // a tick where that checksum actually differs from the previous tick. Non-blocking (own
  // rAF loop, page keeps running) and agnostic to which drawing API or screen region the
  // build uses.
  //
  // __canvasFramesAlive is a heartbeat (last tick's performance.now()), and
  // __rearmCanvasProbe restarts the loop -- a defensive pair added in round 2 after a report
  // that mid-session screenshots zero the fps reading. Measured directly (see grader.py's
  // shot_canvas() and the round-2 fix report): a single save_screenshot() on a plain fixture
  // did NOT stop this loop, so the pair is insurance against a browser/version where it might,
  // not a fix for a reproduced stall of this specific mechanism.
  // The read is BOUNDED, and it is bounded by downscaling rather than by cropping. Reading
  // w*h pixels out of the live canvas on every rAF tick makes the instrument slow the game it
  // is scoring: a 1920x1080 build pays an 8 MB GPU->JS copy 60 times a second and drops frames
  // it would otherwise have drawn, so the grader would be reporting its own overhead. But a
  // CROPPED read is not the answer -- measured 2026-09-10, a centered 320x240 patch dropped
  // fx-good from 60 fps to 12-24 and fx-slow from 10 to 3.7, because whatever moves outside
  // the patch is invisible to it. drawImage into a fixed 128x128 offscreen canvas keeps the
  // WHOLE picture in view (every pixel contributes to the downsample) at a constant 64 KB per
  // tick no matter how large the canvas is. Spec 3.1 step 8 calls for a small pixel sample of
  // the canvas; this is that sample, taken over all of it.
  const PW = 128, PH = 128;
  let c = null, lastSum = null, pcv = null, pctx = null;
  function startTick() {
    (function tick() {
      window.__canvasFramesAlive = performance.now();
      if (!c) c = document.querySelector('canvas');
      if (c) {
        const g = c.getContext && c.getContext('2d');
        if (g) {
          try {
            if (!pctx) {
              pcv = document.createElement('canvas'); pcv.width = PW; pcv.height = PH;
              pctx = pcv.getContext('2d', {willReadFrequently: true});
            }
            pctx.drawImage(c, 0, 0, PW, PH);
            const d = pctx.getImageData(0, 0, PW, PH).data;
            // Order-sensitive rolling hash, not a plain sum: downsampling averages, so two
            // different frames can carry the same total brightness while looking nothing
            // alike. Position matters here, and the hash costs the same as the addition did.
            let s = 0;
            for (let i = 0; i < d.length; i += 4) s = (s * 33 ^ (d[i] + d[i + 1] * 3 + d[i + 2] * 7)) | 0;
            if (lastSum !== null && s !== lastSum) window.__canvasFrames++;
            lastSum = s;
          } catch (e) {}
        }
      }
      requestAnimationFrame(tick);
    })();
  }
  window.__rearmCanvasProbe = startTick;
  startTick();
})();
"""

SIG = """
const c = document.querySelector('canvas'); if (!c) return null;
const g = c.getContext('2d'); if (!g) return 'noctx';
const d = g.getImageData(0,0,c.width,c.height).data;
let sum=0, seen=new Set();
for (let i=0;i<d.length;i+=4*97){ sum+=d[i]+d[i+1]+d[i+2]; seen.add((d[i]<<16)|(d[i+1]<<8)|d[i+2]); }
return JSON.stringify({sum:sum, distinct:seen.size, w:c.width, h:c.height});
"""

SYNTH_DRAG = """
const c = document.querySelector('canvas') || document.body;
const pts = []; for (let i = 0; i < 24; i++) pts.push([240 + i * 7, 700 - i * 4]);
const T = (t, x, y) => { try { c.dispatchEvent(new TouchEvent(t, {bubbles:true, cancelable:true,
  touches: t === 'touchend' ? [] : [new Touch({identifier:1, target:c, clientX:x, clientY:y})],
  changedTouches: [new Touch({identifier:1, target:c, clientX:x, clientY:y})]})); } catch (e) {} };
const P = (t, x, y) => c.dispatchEvent(new PointerEvent(t, {bubbles:true, cancelable:true, pointerId:1,
  pointerType:'touch', isPrimary:true, clientX:x, clientY:y, buttons: t === 'pointerup' ? 0 : 1}));
const M = (t, x, y) => c.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, clientX:x, clientY:y, buttons: t === 'mouseup' ? 0 : 1}));
T('touchstart', 240, 700); P('pointerdown', 240, 700); M('mousedown', 240, 700);
for (const [x, y] of pts) { T('touchmove', x, y); P('pointermove', x, y); M('mousemove', x, y); }
"""

CHECKS = ["state_contract", "title_screen", "manual_control", "keyboard_input", "touch_input",
          "kills", "survives_window", "music_playing", "enemies", "level_up", "weapons_4",
          "weapons_8", "weapons_12", "passive_1", "passives_6", "evolution_1", "evolutions_5",
          "bystanders", "nipster_hidden", "nipster_revealed", "boss_spawned", "boss_defeated",
          "stage_2", "victory"]


def _probe_insert_at(raw):
    """Where to splice the probe <script> tag in. After <head> if present; else right after
    <html ...>; else right after <!doctype ...>; else prepend. Prepending unconditionally
    (the original behaviour) pushes the browser into quirks mode for any build that omits
    <head> -- rare, but real (a minimal single-file canvas demo can skip it), and quirks
    mode changes box-sizing/layout in ways that could make an otherwise-working build fail
    checks that have nothing to do with its actual code."""
    low = raw.lower()
    i = low.find(b"<head>")
    if i >= 0:
        return i + len(b"<head>")
    i = low.find(b"<html")
    if i >= 0:
        close = raw.find(b">", i)
        if close >= 0:
            return close + 1
    i = low.find(b"<!doctype")
    if i >= 0:
        close = raw.find(b">", i)
        if close >= 0:
            return close + 1
    return 0


def serve(directory):
    """HTTP statt file:// (snap-Firefox liest nicht ausserhalb $HOME; Multi-Datei-Builds
    brauchen echte URLs). index.html bekommt die Probe in den <head> injiziert."""
    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k): pass
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/__probe.js":
                body = PROBE_JS.encode(); self.send_response(200)
                self.send_header("Content-Type", "application/javascript"); self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body); return
            if path in ("/", "/index.html"):
                raw = open(os.path.join(directory, "index.html"), "rb").read()
                tag = b'<script src="/__probe.js"></script>'
                pos = _probe_insert_at(raw)
                body = raw[:pos] + tag + raw[pos:]
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body); return
            return super().do_GET()
    handler = functools.partial(H, directory=directory)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def build_driver():
    o = Options()
    o.add_argument("--headless"); o.add_argument("--width=500"); o.add_argument("--height=900")
    o.set_preference("dom.w3c_touch_events.enabled", 1)
    o.set_preference("media.autoplay.default", 0)         # Audio darf ohne Geste starten
    o.set_preference("media.autoplay.blocking_policy", 0)
    d = webdriver.Firefox(service=Service(DRIVER), options=o)
    d.set_window_size(500, 900); d.set_page_load_timeout(60)
    # Selenium 4.47's webdriver.Firefox.__init__(self, options=None, service=None,
    # keep_alive=True) does not accept a client_config= kwarg (checked
    # .venv/lib/python3.12/site-packages/selenium/webdriver/firefox/webdriver.py directly --
    # no such parameter; it builds its own FirefoxRemoteConnection internally with no
    # extension point for one). So the timeout is set on the already-built command
    # executor's ClientConfig after construction instead. Confirmed this actually takes
    # effect per command, not just at HTTP-pool-creation time: RemoteConnection._request()
    # reads self._client_config.timeout fresh on every single call
    # (`self._conn.request(..., timeout=self._client_config.timeout)`), and a live test
    # (execute_script against a page busy-looping its JS thread) raised
    # urllib3.exceptions.ReadTimeoutError at almost exactly the configured value.
    d.command_executor._client_config.timeout = COMMAND_TIMEOUT_S
    return d


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

def state(d):
    return d.execute_script("return window.__state||null;") or {}

def _typed_field(s, field, expected_type, bad_fields=None, notes=None):
    """A __state field that's supposed to be a nested object (nipsters, bosses, player) or a
    list (weapons, passives, evolutions), but a build can report it as a plain scalar instead
    -- observed live (cc-sonnet round 4): `bosses: 0`, `nipsters: 0` as plain numbers, which
    crashed evaluate() outright on the very next `.get()` (`AttributeError: 'int' object has
    no attribute 'get'`) or `len()` (`TypeError: object of type 'int' has no len()`). Missing
    (None) is the ordinary "not present yet" case and stays silent; a present-but-wrong-type
    value is tolerated as empty, and logged once per field (not once per sample, when a
    dedup set and notes list are given -- pos() calls this without either, since it runs many
    times per grading and a crash there would be far more damaging than a missing note)."""
    v = s.get(field)
    empty = expected_type()
    if v is None or isinstance(v, expected_type):
        return v if v is not None else empty
    if bad_fields is not None and notes is not None and field not in bad_fields:
        bad_fields.add(field)
        kind = "object" if expected_type is dict else "array"
        notes.append(f"state.{field} is not an {kind} (got {type(v).__name__})")
    return empty

def pos(st):
    p = _typed_field(st or {}, "player", dict)
    return _num(p.get("x")), _num(p.get("y"))

def sig(d):
    r = d.execute_script(f"return (function(){{{SIG}}})();")
    return None if r in (None, "noctx") else json.loads(r)

def measure_fps(d, dur=0.8):
    """Real wall-clock delta of the canvas-changed-frame counter (see PROBE_JS) --
    non-blocking, so the page's own animation keeps running while we sample it."""
    a = d.execute_script("return window.__canvasFrames||0;"); t0 = time.time()
    time.sleep(dur)
    b = d.execute_script("return window.__canvasFrames||0;"); t1 = time.time()
    return (b - a) / (t1 - t0) if t1 > t0 else 0.0


def shot(d, shots, name, out):
    """A real window screenshot -- used only as shot_canvas()'s fallback now (see below)."""
    if shots:
        os.makedirs(shots, exist_ok=True)
        p = os.path.join(shots, f"{name}.png")
        try:
            d.save_screenshot(p); out.append(p)
        except Exception:
            pass


def rearm_probe_if_stalled(d, notes, max_age_ms=500):
    """Round-2 fix: after every mid-session capture, check the fps probe's heartbeat
    (window.__canvasFramesAlive, see PROBE_JS) and restart its rAF loop if it has gone
    stale. Cheap insurance against a stall from taking a picture, on a browser/version where
    that turns out to happen -- not something reproduced for this mechanism (see the round-2
    fix report), but the loop restart is idempotent and harmless if it never fires."""
    try:
        age = d.execute_script("return performance.now() - (window.__canvasFramesAlive || 0);")
        if age is None or age > max_age_ms:
            d.execute_script("if (window.__rearmCanvasProbe) window.__rearmCanvasProbe();")
            notes.append(f"fps probe heartbeat was stale ({age}ms); re-armed")
    except Exception:
        pass


def shot_canvas(d, shots, name, out, notes=None):
    """Capture the game canvas itself via canvas.toDataURL() instead of a window-level
    save_screenshot(). Added in round 2: a report that fps reads 0 whenever --shots is used
    hypothesised that save_screenshot() stalls or detaches the page's requestAnimationFrame
    loop. That was tested directly (a single save_screenshot() mid-session on a plain,
    always-animating fixture) and did NOT reproduce a stall there, but toDataURL is strictly
    better anyway: it is the actual game canvas rather than the whole browser window, it does
    not need a compositor round-trip, and it cannot possibly interact with rAF the way a
    window capture theoretically could. Falls back to a window screenshot if there is no
    canvas, or toDataURL throws (e.g. the canvas got tainted by a cross-origin draw)."""
    if not shots:
        return
    try:
        data_url = d.execute_script("""
          const c = document.querySelector('canvas');
          if (!c) return null;
          try { return c.toDataURL('image/png'); } catch (e) { return null; }
        """)
        if data_url and data_url.startswith("data:image/png;base64,"):
            os.makedirs(shots, exist_ok=True)
            raw = base64.b64decode(data_url.split(",", 1)[1])
            p = os.path.join(shots, f"{name}.png")
            with open(p, "wb") as f:
                f.write(raw)
            out.append(p)
            if notes is not None:
                rearm_probe_if_stalled(d, notes)
            return
    except Exception:
        pass
    shot(d, shots, name, out)


def touch_threshold(drift):
    """ONE threshold, used both to decide whether the webdriver touch path 'worked' and,
    later, whether touch_input itself passes -- a move that clears the first but not the
    second (previously: >5px vs >max(40, 3*drift)) would get credited to 'webdriver-touch'
    without ever trying the synthetic fallback that might have actually moved the player
    past the real bar."""
    return max(40.0, 3 * drift)


def touch_drag(d, notes, before, drift):
    """Echte Touch-Pointer-Action; bewegt sie den Spieler nicht (Browser traegt sie nicht,
    oder feuert keine Events, die die Seite sieht), dann alle drei Event-APIs synthetisch --
    damit KEINE Bauform bevorzugt wird. Gibt den Pfad zurueck, der gewirkt hat."""
    try:
        ab = ActionBuilder(d, mouse=PointerInput("touch", "finger"))
        pa = ab.pointer_action
        pa.move_to_location(240, 700); pa.pointer_down()
        for i in range(24):
            pa.move_to_location(240 + i * 7, 700 - i * 4)
        pa.pointer_up(); ab.perform(); time.sleep(0.6)
        after = pos(state(d))
        if max(abs(after[0] - before[0]), abs(after[1] - before[1])) > touch_threshold(drift):
            return "webdriver-touch"
        notes.append("webdriver touch action did not clear the touch_input bar; synthetic touch+pointer+mouse")
    except Exception as exc:
        notes.append(f"touch action unavailable ({type(exc).__name__}); synthetic touch+pointer+mouse")
    d.execute_script(SYNTH_DRAG)
    return "synthetic-3way"


def tap(d, notes):
    """Tap per WebDriver-Touch; wenn die Seite danach nicht laeuft, alle drei Event-APIs
    synthetisch. Der Rueckgabewert sagt, welcher Weg gewirkt hat."""
    try:
        ab = ActionBuilder(d, mouse=PointerInput("touch", "finger"))
        ab.pointer_action.move_to_location(250, 400); ab.pointer_action.pointer_down(); ab.pointer_action.pointer_up()
        ab.perform(); time.sleep(0.5)
        if state(d).get("mode") == "running":
            return "webdriver-touch"
    except Exception as exc:
        notes.append(f"touch tap action unavailable ({type(exc).__name__})")
    d.execute_script("""const c=document.querySelector('canvas')||document.body;
      for (const [C,t] of [[PointerEvent,'pointerdown'],[PointerEvent,'pointerup'],[MouseEvent,'mousedown'],[MouseEvent,'mouseup'],[MouseEvent,'click']])
        c.dispatchEvent(new C(t,{bubbles:true,cancelable:true,clientX:250,clientY:400,pointerId:1,pointerType:'touch',isPrimary:true}));
      try{c.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,cancelable:true,touches:[new Touch({identifier:1,target:c,clientX:250,clientY:400})],changedTouches:[new Touch({identifier:1,target:c,clientX:250,clientY:400})]}));
          c.dispatchEvent(new TouchEvent('touchend',{bubbles:true,cancelable:true,touches:[],changedTouches:[new Touch({identifier:1,target:c,clientX:250,clientY:400})]}));}catch(e){}""")
    return "synthetic-3way"


def title_and_start(d, port, shots, notes, out):
    """Titelbild sichtbar? Start per Taste UND per Touch?"""
    res = {"title": False, "start_key": False, "start_touch": False}
    d.get(f"http://127.0.0.1:{port}/index.html"); time.sleep(2.0)
    shot_canvas(d, shots, "title", out, notes)
    st = state(d); s = sig(d)
    res["title"] = (st.get("mode") == "title") and bool(s) and s["distinct"] >= 3
    notes.append(f"title screen: mode={st.get('mode')!r}, colours={s['distinct'] if s else 0} -- {'ok' if res['title'] else 'missing'}")
    try:
        ActionChains(d).move_to_element(d.find_element(By.TAG_NAME, "body")).click().perform()
    except Exception:
        pass
    if state(d).get("mode") == "running":
        # The focus click itself started the run (a build that treats any click/tap on the
        # canvas as "start", e.g. fx-mouse) -- if we pressed Enter now it would prove
        # nothing about the key, since the run is already going. Reload back to the title
        # screen and focus WITHOUT touching the canvas, so Enter alone is what gets tested.
        notes.append("focus click already started the run; reloading to isolate key-start")
        d.get(f"http://127.0.0.1:{port}/index.html"); time.sleep(2.0)
        try:
            d.execute_script("document.body.focus();")
        except Exception:
            pass
    ActionChains(d).key_down(Keys.ENTER).key_up(Keys.ENTER).perform(); time.sleep(0.8)
    res["start_key"] = state(d).get("mode") == "running"
    d.get(f"http://127.0.0.1:{port}/index.html"); time.sleep(1.5)
    path = tap(d, notes); time.sleep(0.8)
    res["start_touch"] = state(d).get("mode") == "running"
    notes.append(f"start: key {'ok' if res['start_key'] else 'no'}, touch {'ok' if res['start_touch'] else 'no'} ({path})")
    return res


def input_session(d, port, notes):
    """?autoplay=0: bewegen Tasten und ein Touch-Drag den Spieler, gegen die eigene Drift?"""
    d.get(f"http://127.0.0.1:{port}/index.html?autoplay=0"); time.sleep(1.5)
    ActionChains(d).key_down(Keys.ENTER).key_up(Keys.ENTER).perform(); time.sleep(1.0)
    st = state(d)
    if "player" not in st:
        notes.append("no __state.player under ?autoplay=0 -- input not measurable")
        return False, False, False, "none"
    manual = st.get("autoplay") is False
    x0, y0 = pos(st); time.sleep(1.0); xd, yd = pos(state(d))
    drift = max(abs(xd - x0), abs(yd - y0), 1.0)
    try:
        ActionChains(d).move_to_element(d.find_element(By.TAG_NAME, "body")).click().perform()
    except Exception:
        pass

    def hold(key, seconds=1.2):
        b = pos(state(d)); ActionChains(d).key_down(key).perform(); time.sleep(seconds)
        ActionChains(d).key_up(key).perform(); time.sleep(0.3); a = pos(state(d))
        return a[0] - b[0]

    dx_r = hold("d")
    if abs(dx_r) <= 3 * drift: dx_r = hold(Keys.ARROW_RIGHT)
    dx_l = hold("a")
    if abs(dx_l) <= 3 * drift: dx_l = hold(Keys.ARROW_LEFT)
    kb = dx_r > 3 * drift and dx_l < -3 * drift
    notes.append(f"keys: right {dx_r:+.1f}, left {dx_l:+.1f} against drift {drift:.1f} -- {'works' if kb else 'DEAD'}")

    before = pos(state(d)); path = touch_drag(d, notes, before, drift); time.sleep(1.0); after = pos(state(d))
    moved = max(abs(after[0] - before[0]), abs(after[1] - before[1]))
    touch = moved > touch_threshold(drift)
    notes.append(f"touch drag moved player {moved:.1f} via {path} -- {'works' if touch else 'DEAD'}")
    return kb, touch, manual, path


def play_session(d, port, seconds, gate_seconds, shots, notes, out):
    """?autoplay=1 ueber das Fenster: __state alle 2 s + Canvas-Pruefsumme; fps aus Bursts."""
    obs = {"loaded": False, "animates": False, "samples": [], "fps": 0.0, "errs": [], "gate": None,
           "audio": {}, "start": None, "shot_t": {}}
    d.get(f"http://127.0.0.1:{port}/index.html?autoplay=1"); time.sleep(3)
    errs = d.execute_script("return window.__errs||[];")
    if errs:
        notes.append(f"JS error on load: {errs[0][:140]}"); obs["errs"] = errs; return obs
    obs["loaded"] = True
    s0 = sig(d)
    if not s0 or s0["distinct"] < 3:
        notes.append("no canvas or canvas blank"); return obs
    time.sleep(2); s1 = sig(d)
    if not (s1 and s1["sum"] != s0["sum"]):
        notes.append("canvas renders but never changes -- no running loop"); return obs
    obs["animates"] = True
    st = state(d)
    if not st:
        notes.append("window.__state absent"); return obs
    obs["start"] = dict(st)
    t0 = time.time(); last_sum = s1["sum"]; fps_samples = []; next_burst = 10; gate_read = False
    # One flag per shot instead of `abs(el - target) < 1.5`: the loop body sleeps 2 s and a
    # slow iteration (state read + getImageData + an fps burst) takes 2.5-3.5 s, so the
    # +-1.5 s window could be stepped straight over and the shot never taken -- silently, with
    # the round's screenshot strip simply missing a picture. `el >= target and not taken`
    # cannot be missed. It also drops the `"t30" not in "".join(out)` substring probe, which
    # searched the accumulated PATHS and would have matched any workdir containing "t30".
    shot_taken = {"t30": False, "t120": False}
    t30_target = min(30, seconds / 4); t120_target = min(120, seconds / 2)
    while time.time() - t0 < seconds:
        time.sleep(2)
        try:
            cur = state(d); s = sig(d) or {"sum": last_sum}
            obs["samples"].append({"t": round(time.time() - t0, 1), "state": cur, "changed": s["sum"] != last_sum})
            last_sum = s["sum"]
            el = time.time() - t0
            if not gate_read and el >= gate_seconds:
                obs["gate"] = cur; gate_read = True
            if el >= next_burst:
                fps_samples.append(measure_fps(d))
                next_burst += 10
            if el >= t30_target and not shot_taken["t30"]:
                shot_taken["t30"] = True
                shot_canvas(d, shots, "t30", out, notes)
                obs["shot_t"]["t30"] = round(el, 1)
                obs["audio"] = d.execute_script("const a=window.__audio||{}; return {contexts:a.contexts||0, nodes:a.nodes||{}, state:a.ctx?a.ctx.state:null, resumed:a.resumed||0};")
            if el >= t120_target and not shot_taken["t120"]:
                shot_taken["t120"] = True
                shot_canvas(d, shots, "t120", out, notes)
                # The file keeps the name "t120", but with window_s drawn from [180, 300] the
                # target is min(120, seconds/2) -- 90 s at a 180 s window. Record the second
                # the picture was actually taken so nothing downstream claims 120.
                obs["shot_t"]["t120"] = round(el, 1)
        except Exception as exc:
            # A dying tab (crash, OOM-killed content process, or -- round 3 -- a build that
            # pegs its JS thread and every WebDriver command against it now hangs until
            # COMMAND_TIMEOUT_S) must return the partial obs collected so far, not blow up the
            # whole run and lose it -- the caller can still score a truncated but real session
            # instead of a blank harness error.
            notes.append(f"tab died mid-session at {round(time.time() - t0, 1)}s: {type(exc).__name__}: {str(exc)[:120]}")
            dead = True
            break
    else:
        dead = False
    # Round 3: every step from here on is best-effort. If the loop already timed out above,
    # the session is known dead -- one more command on the same connection would just cost
    # another full COMMAND_TIMEOUT_S for nothing, so the rest of this phase is skipped
    # outright rather than retried. And if the loop finished clean but ONE of these still
    # times out (the hang can just as well start right after the last sample), the remaining
    # steps stop trying too, on the same reasoning: a note is kept per skipped step, the
    # samples already collected are kept, and a result is still emitted either way.
    if dead:
        notes.append("end phase skipped: session already dead")
    else:
        try:
            shot_canvas(d, shots, "end", out, notes)
        except Exception as exc:
            notes.append(f"end phase timed out: end shot ({type(exc).__name__})")
            dead = True
    if not dead and not obs["audio"]:
        try:
            obs["audio"] = d.execute_script("const a=window.__audio||{}; return {contexts:a.contexts||0, nodes:a.nodes||{}, state:a.ctx?a.ctx.state:null, resumed:a.resumed||0};")
        except Exception as exc:
            notes.append(f"end phase timed out: final audio read ({type(exc).__name__})")
            dead = True
    obs["fps"] = sorted(fps_samples)[len(fps_samples) // 2] if fps_samples else 0.0
    if not dead:
        try:
            obs["errs"] = d.execute_script("return window.__errs||[];")
        except Exception as exc:
            notes.append(f"end phase timed out: final errors read ({type(exc).__name__})")
    if obs["gate"] is None:
        obs["gate"] = obs["samples"][-1]["state"] if obs["samples"] else {}
    notes.append(f"autoplay {int(time.time()-t0)}s over {len(obs['samples'])} samples, fps {obs['fps']:.1f}, audio {obs['audio']}")
    if obs["errs"]:
        notes.append(f"runtime error during play: {obs['errs'][0][:140]}")
    return obs


def evaluate(obs, ts, kb, touch, manual, gate_seconds, notes):
    """Beide Zahlen, mit Plausibilitaetsregeln. Jede Verwerfung landet in rejected[]."""
    rejected = []
    rejected_fields = set()  # dedup: log the first unchanged-sample rise per field ONCE, not every sample
    bad_type_fields = set()  # dedup: log a __state field reported as the wrong type ONCE (see _typed_field)
    samples = obs["samples"]; start = obs.get("start") or {}
    peak = {}
    def bump(k, v): peak[k] = max(peak.get(k, 0), v)
    def gate(i, changed, k, v, label=None):
        """A peak value only ever rises from a sample where the canvas itself changed --
        otherwise a build that freezes the picture but keeps writing __state could still
        earn every check that depends on peak[k] (enemies, bystanders, stage, nipsters,
        bosses -- and, via boss_defeated_at, stage_2/victory). A single coincidental
        "changed" sample right at a freeze boundary can still let one bogus rise through
        (see the kills_confirms note below), so this only guards against the common case:
        self-reported numbers climbing while the picture never moves again. The first such
        rise per field is still recorded, once, so a real regression is visible without
        spamming rejected[] once per remaining sample for the rest of the run."""
        label = label or k
        if changed:
            bump(k, v)
        elif v > peak.get(k, 0) and k not in rejected_fields:
            rejected_fields.add(k)
            rejected.append(f"sample {i}: {label} rose to {v} while the canvas did not change")
    boss_defeated_at = None; level_up_at = None; kills_confirms = 0
    for i, smp in enumerate(samples):
        s = smp["state"] or {}; changed = smp["changed"]
        # Ein einzelner "changed"-Sample an der Grenze (state und Canvas werden nacheinander
        # gepollt, ~2s auseinander) kann einen Selbstauskunfts-Sprung mitnehmen, der in
        # Wahrheit erst NACH dem letzten echten Redraw passiert ist (beobachtet an
        # fx-frozen: das Canvas friert bei elapsed>8 ein, aber genau der Sample, der diese
        # Grenze ueberstreicht, zeigt noch "changed" UND schon den ersten eingefrorenen
        # Kill-Wert). Deshalb zaehlt ein Kill erst als erwiesen, wenn er in mindestens zwei
        # verschiedenen "changed"-Samples ueber dem bisherigen Höchststand bestaetigt wird --
        # ein echt laufendes Spiel liefert das binnen Sekunden, ein eingefrorenes nie zweimal.
        if changed and _num(s.get("kills")) > peak.get("kills", 0): kills_confirms += 1
        gate(i, changed, "kills", _num(s.get("kills")))
        gate(i, changed, "level", _num(s.get("level")))
        if level_up_at is None and _num(s.get("level")) >= 2 and changed: level_up_at = i
        for k in ("enemies", "bystanders", "stage"): gate(i, changed, k, _num(s.get(k)))
        for k in ("weapons", "passives", "evolutions"):
            gate(i, changed, k, len(_typed_field(s, k, list, bad_type_fields, notes)))
        n = _typed_field(s, "nipsters", dict, bad_type_fields, notes)
        gate(i, changed, "nipsters_hidden", _num(n.get("hidden")), label="nipsters.hidden")
        gate(i, changed, "nipsters_revealed", _num(n.get("revealed")), label="nipsters.revealed")
        b = _typed_field(s, "bosses", dict, bad_type_fields, notes)
        gate(i, changed, "bosses_spawned", _num(b.get("spawned")), label="bosses.spawned")
        prev_defeated = peak.get("bosses_defeated", 0)
        gate(i, changed, "bosses_defeated", _num(b.get("defeated")), label="bosses.defeated")
        if boss_defeated_at is None and peak.get("bosses_defeated", 0) > prev_defeated:
            boss_defeated_at = i
        if s.get("ended") == "victory":
            if boss_defeated_at is not None and boss_defeated_at < i: peak["victory"] = True
            else: rejected.append(f"sample {i}: victory reported with no boss defeated in an earlier sample")
    if peak.get("stage", 0) >= 2 and boss_defeated_at is None:
        rejected.append("stage>=2 reported without any boss defeated"); peak["stage"] = 1
    for k in ("weapons", "passives", "evolutions"):
        if peak.get(k, 0) > (1 if k == "weapons" else 0) and level_up_at is None:
            rejected.append(f"{k} grew without a level-up"); peak[k] = 1 if k == "weapons" else 0
    end = samples[-1] if samples else {"state": {}, "changed": False}
    # RULING (final review M1, 2026-09-10). Spec 3.4 says kills count when the canvas checksum
    # changed between two samples -- ONE changed sample. This asks for TWO, deliberately, and
    # the rule stays: fx-frozen animates for ~8 s and only then freezes while it keeps
    # incrementing kills, so the single sample straddling the freeze boundary is genuinely
    # "changed" and a one-confirmation rule credits it a kill it never showed (gate 4 would
    # pass and the fixture's whole point -- rung 4 must fail -- is lost). The cost of the
    # stricter rule is borne by a real build that scores its first and only kill in the last
    # 2 s of a 180-300 s window; that build fails rung 4 while a slower one passes. Judged the
    # smaller error, and it is never silent: the shortfall is written to rejected[] with the
    # confirmation count, so the record says which rule refused the kill.
    killed = peak.get("kills", 0) > _num(start.get("kills")) and kills_confirms >= 2
    if peak.get("kills", 0) > _num(start.get("kills")) and kills_confirms < 2:
        rejected.append(f"kills rose to {peak.get('kills', 0)} but only {kills_confirms} sample(s) confirmed it against a changing canvas -- not enough to call it plausible")
    progressed = killed or peak.get("level", 0) > _num(start.get("level"))
    gate_st = obs.get("gate") or {}
    survived_gate = bool(gate_st.get("alive")) and not obs["errs"]
    st0 = obs.get("start") or {}
    core = ("mode", "alive", "kills", "level", "elapsed", "weapons", "enemies", "player", "autoplay")
    au = obs.get("audio") or {}
    # music_playing judges what the BUILD did, not whether sound came out of speakers: the
    # grading box has no audio backend at all, so ctx.state is stuck at 'suspended' forever
    # regardless of the build (confirmed: real WebDriver clicks, resume() called synchronously
    # inside a genuine click handler, several cubeb/ALSA overrides -- none ever moved it).
    # Gating on ctx.state === 'running' here would be dead by construction, the audio mirror
    # of v2's vacuous rAF-tick fps60. So this checks the graph was actually built (oscillator
    # through a wave shaper) AND actually started (OscillatorNode.start called) AND the build
    # at least attempted to unblock it (resume() called) or never tore the context down.
    # `state` is still recorded in audio{} for the record, just not used as the criterion.
    nodes = au.get("nodes") or {}
    music = au.get("contexts", 0) >= 1 \
            and nodes.get("createOscillator", 0) >= 1 and nodes.get("createWaveShaper", 0) >= 1 \
            and nodes.get("oscillatorStart", 0) >= 1 \
            and (au.get("resumed", 0) >= 1 or au.get("state") != "closed")
    c = {
        "state_contract": bool(st0) and all(k in st0 for k in core),
        "title_screen": ts["title"] and ts["start_key"] and ts["start_touch"],
        "manual_control": manual, "keyboard_input": kb, "touch_input": touch,
        "kills": killed,
        "survives_window": (bool((end["state"] or {}).get("alive")) and end["changed"]) or (peak.get("victory") is True),
        "music_playing": music,
        "enemies": peak.get("enemies", 0) > 0, "level_up": peak.get("level", 0) >= 2,
        "weapons_4": peak.get("weapons", 0) >= 4, "weapons_8": peak.get("weapons", 0) >= 8, "weapons_12": peak.get("weapons", 0) >= 12,
        "passive_1": peak.get("passives", 0) >= 1, "passives_6": peak.get("passives", 0) >= 6,
        "evolution_1": peak.get("evolutions", 0) >= 1, "evolutions_5": peak.get("evolutions", 0) >= 5,
        "bystanders": peak.get("bystanders", 0) > 0,
        "nipster_hidden": peak.get("nipsters_hidden", 0) > 0, "nipster_revealed": peak.get("nipsters_revealed", 0) > 0,
        "boss_spawned": peak.get("bosses_spawned", 0) > 0, "boss_defeated": peak.get("bosses_defeated", 0) > 0,
        "stage_2": peak.get("stage", 0) >= 2, "victory": peak.get("victory") is True,
    }
    gate = 0
    for earned in (obs["loaded"], obs["animates"], kb and touch, killed, survived_gate and progressed):
        if not earned: break
        gate += 1
    if gate < 5:
        notes.append("gate stops at %d: %s" % (gate, ["did not load", "does not animate", "input not wired",
                     "no plausible kills under autoplay", f"did not survive {gate_seconds}s with progress"][gate]))
    for r in rejected: notes.append("rejected: " + r)
    return gate, c, peak, rejected


def _pid_descendants(pid):
    """Every descendant of pid, found by walking /proc/<pid>/task/*/children -- real kernel
    parent-child links, never a `pkill -f` pattern match, which on a shared box could hit an
    unrelated process (another build's browser, someone else's session) that merely happens
    to have a matching string in its command line."""
    out, frontier, seen = [], [pid], set()
    while frontier:
        p = frontier.pop()
        if p in seen:
            continue
        seen.add(p)
        try:
            task_dir = f"/proc/{p}/task"
            for tid in os.listdir(task_dir):
                try:
                    with open(f"{task_dir}/{tid}/children") as f:
                        kids = [int(x) for x in f.read().split()]
                except Exception:
                    kids = []
                for k in kids:
                    if k not in seen:
                        out.append(k); frontier.append(k)
        except Exception:
            pass
    return out


def _kill_pids(pids, notes, grace_s=5):
    """SIGTERM every pid still alive, then SIGKILL anything still standing after grace_s.
    PID-only, by exact number -- see _pid_descendants(). A no-op (no note) when everything's
    already gone, which is the common case: this runs unconditionally after every teardown as
    a final sweep, not only when d.quit() is known to have failed."""
    live = [p for p in pids if os.path.exists(f"/proc/{p}")]
    if not live:
        return
    for p in live:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Poll for the grace period rather than sleeping it unconditionally: SIGKILL after 5s is
    # the ceiling finding 3 specifies, not a fixed cost every teardown has to pay when SIGTERM
    # alone finishes well within it (the common case, once processes actually get signalled).
    deadline = time.time() + grace_s
    still = [p for p in live if os.path.exists(f"/proc/{p}")]
    while still and time.time() < deadline:
        time.sleep(0.3)
        still = [p for p in still if os.path.exists(f"/proc/{p}")]
    for p in still:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    notes.append(f"force-killed leftover browser processes after teardown: {live}"
                 + (f" (SIGKILL needed for {still})" if still else ""))


def score(path, seconds, gate_seconds, shots, seed):
    notes, shots_out = [], []
    httpd, port = serve(os.path.dirname(os.path.abspath(path)))
    # Each resource gets its own try/finally, nested, rather than one shared finally for
    # both: if build_driver() itself raises, httpd was already up and must still be shut
    # down; and if d.quit() itself raises, that must not skip httpd.shutdown() either (a
    # single "finally: d.quit(); httpd.shutdown()" line loses the second call whenever the
    # first one throws).
    try:
        d = build_driver()
        try:
            ts = title_and_start(d, port, shots, notes, shots_out)
            kb, touch, manual, ipath = input_session(d, port, notes)
            obs = play_session(d, port, seconds, gate_seconds, shots, notes, shots_out)
            gate, checks, peak, rejected = evaluate(obs, ts, kb, touch, manual, gate_seconds, notes)
            return {"gate": gate, "capability": {"passed": sum(checks.values()), "total": len(CHECKS), "checks": checks},
                    "span": None, "fps": round(obs["fps"], 1), "window_s": seconds, "seed": seed, "rejected": rejected,
                    "audio": obs.get("audio") or {}, "screenshots": shots_out, "notes": notes, "peak": peak,
                    "input_path": ipath, "samples": len(obs["samples"]),
                    "shot_t": obs.get("shot_t") or {}}
        finally:
            # Round 3 teardown. Record the whole browser process tree (geckodriver + every
            # Firefox process under it -- launcher, content processes, GPU/RDD/socket/utility
            # processes) BEFORE attempting to quit, since a hung d.quit() can't be trusted to
            # leave anything to introspect afterward, and the fx-busy-after-end selftest
            # checks these exact pids are gone once teardown is done.
            gecko_pid = d.service.process.pid if d.service.process else None
            browser_pids = ([gecko_pid] if gecko_pid else []) + (_pid_descendants(gecko_pid) if gecko_pid else [])
            notes.append(f"browser pids before teardown: {browser_pids}")
            # d.quit() is run in a daemon thread and joined with TEARDOWN_TIMEOUT_S, not
            # called directly with client_config.timeout alone. Measured directly (see the
            # round-3 fix report): reducing client_config.timeout bounds the WebDriver HTTP
            # call inside quit() (confirmed: the DELETE /session request does time out at the
            # reduced value), but quit() -> service.stop() then ALSO calls
            # Service._terminate_process(), which does `self.process.wait(60)` -- a SEPARATE,
            # hardcoded 60 s wait for the geckodriver OS process to exit
            # (selenium/webdriver/common/service.py), entirely outside client_config's
            # reach. Against a genuinely stuck browser this pushed d.quit() past 80s even
            # with client_config.timeout reduced, which alone can blow the
            # "window + ~120s" bound. A real timeout on the join is what actually bounds it:
            # if quit() is not done within TEARDOWN_TIMEOUT_S, this stops waiting on it (the
            # thread is abandoned, not killed -- Python cannot forcibly kill a thread -- but
            # it no longer blocks anything else) and moves straight to the PID-based kill
            # below. A healthy quit() measured well under 1s, so this is not a tight bound on
            # the common case.
            quit_done = threading.Event()
            def _do_quit():
                try:
                    d.command_executor._client_config.timeout = TEARDOWN_TIMEOUT_S
                    d.quit()
                except Exception:
                    pass
                finally:
                    quit_done.set()
            threading.Thread(target=_do_quit, daemon=True).start()
            if not quit_done.wait(TEARDOWN_TIMEOUT_S):
                notes.append(f"d.quit() did not finish within {TEARDOWN_TIMEOUT_S}s; killing browser processes by pid")
            # Unconditional final sweep by the pids recorded above -- cheap (a no-op) when
            # quit() actually worked, and the only thing that reliably closes the loop when
            # it didn't: never a pattern-matching pkill, which could catch an unrelated
            # browser process on this shared machine.
            _kill_pids(browser_pids, notes)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--seconds", type=int, default=0, help="0 = aus --seed in [180,300] ziehen")
    ap.add_argument("--gate-seconds", type=int, default=45)
    ap.add_argument("--seed", default=None)
    ap.add_argument("--shots", default=None, help="Verzeichnis fuer Screenshots")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else str(int(time.time()))
    seconds = a.seconds or random.Random(int(hashlib.sha256(seed.encode()).hexdigest(), 16)).randint(180, 300)
    path = os.path.join(a.workdir, "index.html")
    # Two different zeros, and they must not be confused. A build with no index.html HAS been
    # graded: it scores 0 of 24, legitimately, and belongs in the median. A grader that could
    # not run at all (geckodriver would not spawn, the page never loaded, the session died)
    # has measured NOTHING -- reporting it as 24 failed checks let median_grade keep it (the
    # checks dict is truthy), dragged the round's median down, poisoned span=[0,N] and with it
    # the podium tie rule, and two such flakes drove gate and checklist to 0 with nothing in
    # the record to say the browser, not the build, was at fault. total 0 / checks {} is the
    # shape median_grade filters out; grades_failed carries the count into the round result.
    empty = {"gate": 0, "capability": {"passed": 0, "total": len(CHECKS), "checks": {k: False for k in CHECKS}},
             "span": None, "fps": 0.0, "window_s": seconds, "seed": seed, "rejected": [], "audio": {},
             "screenshots": [], "peak": {}, "input_path": "none", "samples": 0, "shot_t": {}}
    harness_error = dict(empty, capability={"passed": 0, "total": 0, "checks": {}})
    if not os.path.exists(path):
        print(json.dumps(dict(empty, notes=["no index.html"]))); return
    if not os.path.exists(DRIVER):
        sys.exit(f"geckodriver not found at {DRIVER}")
    try:
        res = score(path, seconds, a.gate_seconds, a.shots, seed)
    except Exception as exc:
        res = dict(harness_error, notes=[f"harness error: {type(exc).__name__}: {str(exc)[:160]}"])
    print(json.dumps(res, indent=None if a.json else 2))


if __name__ == "__main__":
    main()

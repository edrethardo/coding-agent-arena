"""OpenAI-kompatibler Durchreicher, der jeden Modellaufruf als Span nach Phoenix schreibt.

WARUM. Die Arena vergleicht Harnesses. Sich auf deren eigene Instrumentierung zu verlassen
heisst, jede Zeile anders zu messen: Hermes' `monitoring` ist laut eigener Beschreibung
"content-free by construction" und liefert keine Agentenspur, sein Langfuse-Plugin schreibt
woandershin, dsh bringt einen eigenen OTel-Exporter mit, pi wieder etwas anderes. Ein Proxy
VOR dem Modell sieht bei allen dasselbe und misst sie damit gleich.

WAS ER AUFZEICHNET, und warum genau das. Drei Fuenf-Stunden-Laeufe sind an Fragen
gescheitert, die aus den Aggregaten nicht zu beantworten waren:

  * Wie viele Werkzeug-Ergebnisse liegen im Prompt, und wie gross sind sie? Hermes' Prunen
    fasst nur Ergebnisse ueber `proactive_prune_min_result_chars` (8000) an -- ob diese
    Schwelle je erreicht wird, war reine Vermutung. Der Proxy zaehlt es.
  * Wie verteilt sich die Antwort auf Reasoning und Inhalt? Aus den vLLM-Zaehlern nicht zu
    trennen; in der rohen Antwort steht es.
  * Wie waechst der Prompt ueber die Runde? Die Rundensumme verbirgt den Verlauf.

STREAMING IST HEILIG. Der vorhandene `nothink-proxy` puffert die ganze Antwort
(`urlopen(...).read()`), was fuer einen einmaligen Aufruf reicht, einen streamenden Agenten
aber blockiert, bis das letzte Token da ist. Hier werden Bytes weitergereicht, sobald sie
ankommen, und nur nebenbei mitgezaehlt.

NIEMALS DEN LAUF GEFAEHRDEN. Jeder Tracing-Fehler wird geschluckt; scheitert der Export,
laeuft der Aufruf trotzdem durch. Ein Messgeraet, das den Versuch kaputtmacht, ist keines.

    python3 tools/trace_proxy.py --upstream http://127.0.0.1:8000 \
        --port 8010 --project arena-proxy
"""
import argparse, http.client, json, os, socket, statistics, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

UPSTREAM = "http://127.0.0.1:8000"
DIALECT = "openai"
TRACER = None
HOP = {"host", "connection", "content-length", "accept-encoding", "transfer-encoding"}
# Antwort-Header, die wir selbst setzen (oder die reine Verbindungsdetails sind) -- alles
# andere (retry-after, die anthropic-ratelimit-* Familie, request-id, ...) muss unveraendert
# beim Harness ankommen, sonst veraendert das Messgeraet dessen Backoff-Verhalten.
RESP_HOP = {"content-type", "content-length", "connection", "transfer-encoding", "keep-alive"}

STATE = {"round": 0, "inflight": 0, "records": [], "lock": threading.Lock(), "row": "", "run_id": "", "stats_dir": None}

# Ein Parse-Versuch, der an nicht-JSON (leerer Body nach einem abgebrochenen Stream, eine
# HTML-Fehlerseite eines Providers) scheitert, darf keine Felder auf None statt 0 setzen --
# sonst reisst eine einzige kaputte Antwort spaeter die ganze Rundenaggregation um (r["x"] + None).
_EMPTY_PARSE = {"prompt_tokens": None, "input_uncached": None, "completion_tokens": None, "cache_read": None,
                "cache_write": None, "reasoning_chars": 0, "text_chars": 0, "tool_use_count": 0, "stop": None}


def _anthropic_prompt_tokens(u):
    """Anthropics `message_start.usage.input_tokens` zaehlt nur den UNCACHED Anteil des
    Prompts -- Cache-Treffer stehen separat in cache_read_input_tokens/cache_creation_input_tokens.
    prompt_tokens muss die Summe aus allen dreien sein, sonst wird z.B. 9 uncached + 40184
    cache_read als "Prompt von 9 Tokens" gebucht und cache_read_share explodiert auf > 1000%."""
    input_uncached = u.get("input_tokens")
    cache_read = u.get("cache_read_input_tokens")
    cache_write = u.get("cache_creation_input_tokens")
    prompt_tokens = input_uncached + (cache_read or 0) + (cache_write or 0) if input_uncached is not None else None
    return prompt_tokens, input_uncached, cache_read, cache_write


def _tracer(project, endpoint):
    global TRACER
    if TRACER is not None:
        return TRACER
    try:
        from phoenix.otel import register
        provider = register(project_name=project, endpoint=f"{endpoint}/v1/traces",
                            auto_instrument=False, batch=True, set_global_tracer_provider=False)
        TRACER = provider.get_tracer("arena.trace_proxy")
    except Exception as exc:
        print(f"trace-proxy: kein Phoenix ({exc}); es wird nur durchgereicht")
        TRACER = False
    return TRACER


def _blocks_chars(content):
    """Zeichen je Blocktyp einer Anthropic-Nachricht (str oder Blockliste)."""
    if isinstance(content, str):
        return {"text": len(content)}
    out = {}
    for b in content or []:
        t = b.get("type", "?")
        if t == "text":
            n = len(b.get("text") or "")
        elif t in ("thinking", "redacted_thinking"):
            # Claude Code haengt bisherige Denkbloecke jede Runde wieder an den Prompt --
            # ungezaehlt sieht der tool_result_share Anteil um ein Vielfaches zu hoch aus.
            n = len(b.get("thinking") or b.get("data") or "")
        elif t == "image":
            src = b.get("source") or {}
            n = len(src.get("data") or "") if isinstance(src, dict) else len(json.dumps(src))
        else:
            v = b.get("content") or b.get("input") or ""
            # tool_result.content ist meist ein String (json.dumps wuerde zwei Anfuehrungs-
            # zeichen mitzaehlen); nur bei Nicht-Strings (z.B. verschachtelte Blockliste) json.dumps.
            n = len(v) if isinstance(v, str) else len(json.dumps(v))
        out[t] = out.get(t, 0) + n
    return out


def describe_request(payload):
    msgs = payload.get("messages") or payload.get("input") or []
    total = tool = 0
    for m in msgs:
        if not isinstance(m, dict):
            total += len(str(m)); continue
        if DIALECT == "anthropic":
            bc = _blocks_chars(m.get("content"))
            total += sum(bc.values()); tool += bc.get("tool_result", 0)
        else:
            c = m.get("content"); n = len(c) if isinstance(c, str) else len(json.dumps(c or ""))
            total += n
            if m.get("role") == "tool" or m.get("type") in ("function_call_output", "computer_call_output"):
                tool += n
    sys_ = payload.get("system")
    total += len(sys_) if isinstance(sys_, str) else len(json.dumps(sys_ or ""))
    return {"messages": len(msgs), "tool_result_chars": tool,
            "tool_result_share": round(tool / total, 3) if total else 0.0,
            "model": payload.get("model", "?"),
            # Steuerfelder, NIE der Inhalt: was hat der Harness tatsaechlich angefordert? Hermes'
            # reasoning_effort erreicht ein vLLM/Qwen-Modell u.U. nie (das chat_template liest
            # chat_template_kwargs.reasoning_effort, Default xhigh), und Claude-Code-Zeilen zeigen
            # 0% Reasoning -- ohne diese Felder laesst sich nicht sagen, was jeder Harness wirklich
            # geschickt hat.
            "req_max_tokens": payload.get("max_tokens") or payload.get("max_completion_tokens"),
            "req_reasoning_effort": payload.get("reasoning_effort"),
            "req_reasoning": payload.get("reasoning"),
            "req_chat_template_kwargs": payload.get("chat_template_kwargs"),
            "req_thinking": payload.get("thinking"),
            "req_temperature": payload.get("temperature"),
            "req_tools": len(payload.get("tools") or [])}


def parse_openai_sse(text):
    reason = content = ""; usage = {}; finish = None
    for line in text.splitlines():
        if not line.startswith("data:"): continue
        part = line[5:].strip()
        if part in ("", "[DONE]"): continue
        try: o = json.loads(part)
        except Exception: continue
        d = ((o.get("choices") or [{}])[0]).get("delta") or {}
        reason += d.get("reasoning") or d.get("reasoning_content") or ""
        content += d.get("content") or ""
        if o.get("usage"): usage = o["usage"]
        fr = ((o.get("choices") or [{}])[0]).get("finish_reason")
        if fr: finish = fr
    # Spec 5.1: "<think>-Segmente als Reasoning". Only some OpenAI-compatible servers put the
    # thinking in a `reasoning`/`reasoning_content` delta field; vLLM without a reasoning
    # parser streams it inline in `content` as <think>...</think>, and then reasoning_chars
    # read 0 while text_chars counted the thinking as answer text -- reasoning_share 0.0 for a
    # model that spent most of its output thinking. Fall back to the inline segments, and only
    # as a fallback: a server that sends both must not have its reasoning counted twice.
    if not reason and "<think>" in content:
        reason, content = _split_think(content)
    return {"prompt_tokens": usage.get("prompt_tokens"), "input_uncached": None,
            "completion_tokens": usage.get("completion_tokens"),
            "cache_read": None, "cache_write": None, "reasoning_chars": len(reason), "text_chars": len(content),
            "tool_use_count": 0, "stop": finish}


def parse_codex_sse(text):
    """Extract Responses/Codex stream usage without retaining request content."""
    r = dict(_EMPTY_PARSE)
    for line in text.splitlines():
        if not line.startswith("data:"): continue
        try: o = json.loads(line[5:].strip())
        except Exception: continue
        typ = o.get("type", "")
        if typ == "response.output_text.delta": r["text_chars"] += len(o.get("delta") or "")
        elif typ in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            r["reasoning_chars"] += len(o.get("delta") or "")
        elif typ == "response.output_item.added" and (o.get("item") or {}).get("type") in ("function_call", "computer_call"):
            r["tool_use_count"] += 1
        elif typ == "response.completed":
            response = o.get("response") or {}; usage = response.get("usage") or {}
            details = usage.get("input_tokens_details") or {}
            r.update(prompt_tokens=usage.get("input_tokens"), completion_tokens=usage.get("output_tokens"),
                     cache_read=details.get("cached_tokens"), stop=response.get("status"))
    return r


def _split_think(content):
    """(reasoning, text) aus inline <think>...</think>-Segmenten.

    Ein abgeschnittener Stream endet mit einem offenen <think> ohne </think>; alles danach
    ist dann Reasoning, nicht Antworttext -- sonst zaehlt ein abgebrochener Denkvorgang als
    Ausgabe. Die Marker selbst gehoeren in keine der beiden Zahlen.
    """
    reason, text, rest = "", "", content
    while True:
        i = rest.find("<think>")
        if i < 0:
            text += rest; break
        text += rest[:i]
        rest = rest[i + len("<think>"):]
        j = rest.find("</think>")
        if j < 0:
            reason += rest; break
        reason += rest[:j]
        rest = rest[j + len("</think>"):]
    return reason, text


def parse_anthropic_sse(text):
    r = {"prompt_tokens": None, "input_uncached": None, "completion_tokens": None, "cache_read": None,
         "cache_write": None, "reasoning_chars": 0, "text_chars": 0, "tool_use_count": 0, "stop": None}
    for line in text.splitlines():
        if not line.startswith("data:"): continue
        try: o = json.loads(line[5:].strip())
        except Exception: continue
        t = o.get("type")
        if t == "message_start":
            u = (o.get("message") or {}).get("usage") or {}
            r["prompt_tokens"], r["input_uncached"], r["cache_read"], r["cache_write"] = _anthropic_prompt_tokens(u)
        elif t == "content_block_start":
            if (o.get("content_block") or {}).get("type") == "tool_use": r["tool_use_count"] += 1
        elif t == "content_block_delta":
            d = o.get("delta") or {}
            if d.get("type") == "thinking_delta": r["reasoning_chars"] += len(d.get("thinking") or "")
            elif d.get("type") == "text_delta": r["text_chars"] += len(d.get("text") or "")
        elif t == "message_delta":
            u = o.get("usage") or {}
            if u.get("output_tokens") is not None: r["completion_tokens"] = u["output_tokens"]
            r["stop"] = (o.get("delta") or {}).get("stop_reason")
    return r


def parse_json_body(text):
    try: o = json.loads(text)
    except Exception: return dict(_EMPTY_PARSE)
    if DIALECT == "anthropic":
        u = o.get("usage") or {}
        prompt_tokens, input_uncached, cache_read, cache_write = _anthropic_prompt_tokens(u)
        r = {"prompt_tokens": prompt_tokens, "input_uncached": input_uncached, "completion_tokens": u.get("output_tokens"),
             "cache_read": cache_read, "cache_write": cache_write,
             "reasoning_chars": 0, "text_chars": 0, "tool_use_count": 0, "stop": o.get("stop_reason")}
        for b in o.get("content") or []:
            if b.get("type") == "thinking": r["reasoning_chars"] += len(b.get("thinking") or "")
            elif b.get("type") == "text": r["text_chars"] += len(b.get("text") or "")
            elif b.get("type") == "tool_use": r["tool_use_count"] += 1
        return r
    if DIALECT == "codex":
        u = o.get("usage") or {}; details = u.get("input_tokens_details") or {}
        r = {"prompt_tokens": u.get("input_tokens"), "input_uncached": None,
             "completion_tokens": u.get("output_tokens"), "cache_read": details.get("cached_tokens"),
             "cache_write": None, "reasoning_chars": 0, "text_chars": 0, "tool_use_count": 0,
             "stop": o.get("status")}
        for item in o.get("output") or []:
            typ = item.get("type")
            if typ in ("function_call", "computer_call"): r["tool_use_count"] += 1
            for block in item.get("content") or []:
                if block.get("type") in ("output_text", "text"): r["text_chars"] += len(block.get("text") or "")
                elif block.get("type", "").startswith("reasoning"): r["reasoning_chars"] += len(block.get("text") or "")
        return r
    u = o.get("usage") or {}; m = ((o.get("choices") or [{}])[0]).get("message") or {}
    return {"prompt_tokens": u.get("prompt_tokens"), "input_uncached": None, "completion_tokens": u.get("completion_tokens"),
            "cache_read": None, "cache_write": None, "reasoning_chars": len(m.get("reasoning") or m.get("reasoning_content") or ""),
            "text_chars": len(m.get("content") or ""), "tool_use_count": len(m.get("tool_calls") or []),
            "stop": ((o.get("choices") or [{}])[0]).get("finish_reason")}


def _connect(upstream):
    """Verbindung zum Upstream, plus dessen eigenen Pfad-Praefix (z.B. '/api' in
    'http://host:8000/api'), den `self.path` sonst stillschweigend verschlucken wuerde."""
    u = urlsplit(upstream)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=3600), u.path


def is_model_call(path):
    p = path.split("?")[0].rstrip("/")
    if DIALECT == "openai": return p.endswith("/chat/completions")
    if DIALECT == "codex": return p.endswith("/responses")
    return p.endswith("/v1/messages")


def strip_anthropic_marker(path):
    """Hermes' Config-Validator akzeptiert fuer den Anthropic-Transport nur einen
    anthropic.com-Host oder einen `/anthropic`-Pfad im base_url -- also traegt Hermes ein
    fuehrendes `/anthropic`-Segment auf, das nur als Marker fuer Hermes selbst dient. Der
    echte Upstream (api.anthropic.com) kennt dieses Segment nicht und antwortet mit 404, wenn
    wir es mitschicken. Nur EIN fuehrendes Segment wird entfernt, nur im anthropic-Dialekt,
    und nur wenn es exakt "anthropic" heisst (kein "/anthropicfoo/...").
    """
    if DIALECT != "anthropic": return path
    if path == "/anthropic": return "/"
    if path.startswith("/anthropic/") or path.startswith("/anthropic?"):
        return path[len("/anthropic"):]
    return path


def record(rec):
    with STATE["lock"]:
        STATE["records"].append(rec)
        if STATE["stats_dir"]:
            with open(os.path.join(STATE["stats_dir"], "proxy-requests.jsonl"), "a") as fh:
                fh.write(json.dumps(rec) + "\n")


def retag(old_round, new_round):
    """Verschiebt alle Aufzeichnungen einer Runde auf eine andere Rundennummer, in Speicher
    UND in proxy-requests.jsonl -- sonst widersprechen sich proxy.json (aus dem Speicher) und
    der Ereignisstreifen der Seite (aus der Datei) fuer dieselbe Runde."""
    if old_round is None or new_round is None:
        return 0
    old_round, new_round = int(old_round), int(new_round)
    with STATE["lock"]:
        moved = 0
        for r in STATE["records"]:
            if r["round"] == old_round:
                r["round"] = new_round; moved += 1
        path = os.path.join(STATE["stats_dir"], "proxy-requests.jsonl") if STATE["stats_dir"] else None
        if moved and path and os.path.exists(path):
            try:
                lines = []
                for line in open(path):
                    line = line.strip()
                    if not line: continue
                    try:
                        o = json.loads(line)
                        if o.get("round") == old_round: o["round"] = new_round
                        lines.append(json.dumps(o))
                    except Exception:
                        lines.append(line)
                with open(path, "w") as fh:
                    fh.write("\n".join(lines) + "\n")
            except Exception as exc:
                print(f"trace-proxy: retag of {path} failed ({exc})", flush=True)
    return moved


def _share_mean(pairs):
    """Mittel von reasoning/(reasoning+text) ueber die Aufrufe, die ueberhaupt Zeichen haben."""
    shares = [a / (a + b) for a, b in pairs if (a + b) > 0]
    return round(sum(shares) / len(shares), 3) if shares else None


def aggregate(rnd):
    with STATE["lock"]:
        rs = [r for r in STATE["records"] if r["round"] == rnd]
        inflight_now = STATE["inflight"]
    def med(k):
        v = [r[k] for r in rs if r.get(k) is not None]
        return round(statistics.median(v), 3) if v else None
    def sm(k): return sum(r[k] or 0 for r in rs)
    pt = sm("prompt_tokens"); cr = sm("cache_read")
    codes = {}
    for r in rs: codes[str(r["status"])] = codes.get(str(r["status"]), 0) + 1
    return {"round": rnd, "calls": len(rs), "prompt_tokens_sum": pt,
            "last_call_t": max([r["t"] + r["stream_s"] for r in rs], default=None),   # fuer die Stall-Erkennung des Runners
            "prompt_tokens_max": max([r["prompt_tokens"] or 0 for r in rs], default=0),
            "completion_tokens_sum": sm("completion_tokens"),
            "cache_read_share": round(cr / pt, 3) if pt else None,
            "tool_result_share_mean": round(sum(r["tool_result_share"] for r in rs) / len(rs), 3) if rs else None,
            # Mean over the calls that HAVE a share, not over every call: the sum skipped calls
            # with neither reasoning nor text (a pure tool_use turn -- most of a Claude Code
            # round) while the divisor still counted them, so the figure fell as a row used
            # more tools. None when no call produced either, rather than a 0.0 that reads like
            # "this model never thinks".
            "reasoning_share_mean": _share_mean([(r["reasoning_chars"] or 0, r["text_chars"] or 0) for r in rs]),
            "ttft_median_s": med("ttft_s"), "out_tok_s_median": med("out_tok_s"),
            "inflight_max": max([r["inflight"] for r in rs], default=0),
            # Ein Aufruf, der GERADE streamt, hat noch kein last_call_t -- ein einzelner langer
            # Stream sonst als Stillstand gelesen. Der Runner zaehlt inflight_now > 0 als
            # Lebenszeichen (siehe run_game_bench.wait_round).
            "inflight_now": inflight_now, "status_counts": codes}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    project = "arena-proxy"; phoenix = "http://localhost:6006"

    def _control(self):
        if self.path.startswith("/__arena/round") and self.command == "POST":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            # Optional retag: the runner's exit-9 fallback runs a round TWICE and both attempts
            # POSTed the same round number, so r<n>/proxy.json summed the discarded attempt and
            # the retry. Before the retry the runner moves the first attempt's calls to a tag of
            # its own (round*100+1) -- kept, queryable, and out of the round's numbers.
            moved = retag(body.get("retag_from"), body.get("retag_to"))
            STATE["round"] = int(body.get("round", 0))
            self._json({"ok": True, "round": STATE["round"], "retagged": moved}); return True
        if self.path.startswith("/__arena/stats"):
            params = parse_qs(urlsplit(self.path).query)
            rnd = int(params.get("round", [str(STATE["round"])])[0])
            self._json(aggregate(rnd)); return True
        return False

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode(); self.send_response(status)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def _forward(self, method):
        if self._control(): return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
        call = bool(body) and is_model_call(self.path)
        meta = {}
        if call:
            try: meta = describe_request(json.loads(body))
            except Exception: call = False
        # A LIST of pairs, not a dict: HTTP allows a header name to appear more than once
        # (Set-Cookie on the way back, and on the way out anthropic-beta, which Claude Code
        # sends as several lines). A dict kept only the last one, so the request that reached
        # api.anthropic.com was not the request the harness made -- an instrument must not
        # change what it measures.
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]
        # Runde bei Aufrufbeginn festhalten: ein `/__arena/round`, das waehrend eines laufenden
        # Streams eintrifft, darf den Aufruf nicht nachtraeglich der neuen Runde zuschlagen.
        round_at_call = STATE["round"]
        inflight = None
        if call:
            with STATE["lock"]:
                STATE["inflight"] += 1; inflight = STATE["inflight"]
        t0 = time.time(); ttft = None; collected = []; status = 0; conn = None
        sent_headers = False; truncated = False
        try:
            # http.client statt urllib.request: urlopen() haette jeden Header-Namen mit
            # .title() umgeschrieben und immer ein "Accept-Encoding: identity" untergemogelt,
            # selbst wenn wir es aus HOP entfernt haben -- ein Messgeraet darf das nicht.
            conn, prefix = _connect(UPSTREAM)
            conn.putrequest(method, prefix + strip_anthropic_marker(self.path), skip_accept_encoding=True)
            for k, v in headers: conn.putheader(k, v)
            if body is not None: conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            up = conn.getresponse()
            status = up.status
            ctype = up.getheader("Content-Type", "application/json")
            streaming = "event-stream" in (ctype or "")
            self.send_response(status)
            sent_headers = True   # Statuszeile ist raus -- ab hier keine zweite Antwort mehr moeglich
            self.send_header("Content-Type", ctype)
            for k, v in up.getheaders():
                lk = k.lower()
                if lk in RESP_HOP or (streaming and lk == "cache-control"): continue
                self.send_header(k, v)
            if streaming:
                self.send_header("Cache-Control", "no-cache"); self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = up.read1(8192)
                    if not chunk: break
                    if ttft is None: ttft = time.time() - t0
                    self.wfile.write(chunk); self.wfile.flush(); collected.append(chunk)
            else:
                data = up.read(); ttft = time.time() - t0
                self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data); collected.append(data)
        except Exception as exc:
            if sent_headers:
                # Die echte Antwort ist schon (teilweise) unterwegs -- eine zweite hineinzu-
                # schreiben wuerde den Stream fuer den Client zerstoeren. Nur noch aufzeichnen,
                # was tatsaechlich ankam; der Runner sieht status=<vom Upstream> + truncated=true.
                truncated = True
            else:
                data = json.dumps({"error": f"proxy: {exc}"}).encode(); status = 502
                try:
                    self.send_response(502); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
                except Exception:
                    pass   # Client bereits weg -- auch der Fehlerbericht darf daran nicht scheitern
        finally:
            if call:
                with STATE["lock"]: STATE["inflight"] -= 1
            if conn is not None: conn.close()
        if call:
            self._emit(meta, b"".join(collected), t0, ttft, inflight, status, round_at_call, truncated)

    def _emit(self, meta, raw, t0, ttft, inflight, status, round_at_call, truncated):
        try:
            text = raw.decode("utf-8", "replace"); stream_s = time.time() - t0
            if text.lstrip().startswith(("data:", "event:")):
                r = parse_anthropic_sse(text) if DIALECT == "anthropic" else (parse_codex_sse(text) if DIALECT == "codex" else parse_openai_sse(text))
            else:
                r = parse_json_body(text)
            gen_s = stream_s - (ttft or 0)
            rec = {"t": round(t0, 3), "round": round_at_call, "row": STATE["row"], "run_id": STATE["run_id"], "status": status,
                   "model": meta["model"], "messages": meta["messages"], "tool_result_chars": meta["tool_result_chars"],
                   "tool_result_share": meta["tool_result_share"], "ttft_s": round(ttft, 3) if ttft is not None else None,
                   "stream_s": round(stream_s, 3), "inflight": inflight, "truncated": bool(truncated),
                   **{k: meta.get(k) for k in ("req_max_tokens", "req_reasoning_effort", "req_reasoning",
                                               "req_chat_template_kwargs", "req_thinking", "req_temperature", "req_tools")},
                   "out_tok_s": round((r.get("completion_tokens") or 0) / gen_s, 1) if gen_s > 0 and r.get("completion_tokens") else None,
                   **{k: r.get(k) for k in ("prompt_tokens", "input_uncached", "completion_tokens", "cache_read", "cache_write",
                                            "reasoning_chars", "text_chars", "tool_use_count", "stop")}}
            record(rec)
            tr = _tracer(self.project, self.phoenix)
            if not tr: return
            rs = (rec["reasoning_chars"] or 0) + (rec["text_chars"] or 0)
            attrs = {"openinference.span.kind": "LLM", "llm.model_name": rec["model"], "row": rec["row"], "run_id": rec["run_id"],
                     "round": rec["round"], "status": status, "truncated": rec["truncated"],
                     "llm.token_count.prompt": rec["prompt_tokens"] or 0,
                     "llm.token_count.completion": rec["completion_tokens"] or 0, "cache_read_tokens": rec["cache_read"] or 0,
                     "cache_write_tokens": rec["cache_write"] or 0, "req.messages": rec["messages"],
                     "req.tool_result_chars": rec["tool_result_chars"], "req.tool_result_share": rec["tool_result_share"],
                     "resp.reasoning_chars": rec["reasoning_chars"], "resp.text_chars": rec["text_chars"],
                     "resp.tool_use_count": rec["tool_use_count"], "resp.reasoning_share": round((rec["reasoning_chars"] or 0) / rs, 3) if rs else 0.0,
                     "ttft_s": rec["ttft_s"] or 0.0, "stream_s": rec["stream_s"], "out_tok_s": rec["out_tok_s"] or 0.0, "inflight": inflight,
                     # Steuerfelder des Aufrufs -- OTel-Attribute duerfen keine verschachtelten
                     # Objekte sein, daher json.dumps fuer reasoning/chat_template_kwargs/thinking.
                     "req.max_tokens": rec["req_max_tokens"], "req.reasoning_effort": rec["req_reasoning_effort"],
                     "req.reasoning": json.dumps(rec["req_reasoning"]) if rec["req_reasoning"] is not None else None,
                     "req.chat_template_kwargs": json.dumps(rec["req_chat_template_kwargs"]) if rec["req_chat_template_kwargs"] is not None else None,
                     "req.thinking": json.dumps(rec["req_thinking"]) if rec["req_thinking"] is not None else None,
                     "req.temperature": rec["req_temperature"], "req.tools": rec["req_tools"]}
            with tr.start_as_current_span("llm.call") as sp:
                for k, v in attrs.items():
                    if v is not None: sp.set_attribute(k, v)
        except Exception:
            pass

    def do_POST(self): self._forward("POST")
    def do_GET(self): self._forward("GET")
    def log_message(self, *a): pass


class ThreadingHTTPServerV6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _serve_dual_stack(port, handler_cls):
    """Hermes haelt seine OAuth-Header nur fuer URLs, die "anthropic.com" enthalten --
    `arena-proxy.anthropic.com.localhost` erfuellt das, aber `*.localhost` loest systemweit
    (ohne /etc/hosts-Eintrag) zu ::1 auf, nicht zu 127.0.0.1. Der Proxy muss also auf BEIDEN
    Loopbacks hoeren, im selben Prozess, mit demselben Handler und derselben STATE. Nur
    Loopback -- niemals "::" oder "0.0.0.0" binden."""
    v4 = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    v6 = None
    try:
        v6 = ThreadingHTTPServerV6(("::1", port), handler_cls)
    except OSError as exc:
        # Im anthropic-Dialekt ist ::1 nicht optional: die hermes-claude-Zeilen zeigen auf
        # arena-proxy.anthropic.com.localhost, und *.localhost loest ausschliesslich auf ::1
        # auf. Ohne ::1 kommt kein einziger Aufruf am Proxy an -- frueher nur eine Warnung,
        # der Proxy lief weiter, und die Runde lief still am Messgeraet vorbei. Jetzt stirbt
        # er beim Start, was der Runner als "port busy?" meldet und abbricht.
        if DIALECT == "anthropic":
            v4.server_close()
            raise SystemExit(f"trace-proxy[anthropic]: ::1 nicht bindbar ({exc}) -- die "
                             f"claude-Zeilen erreichen den Proxy nur ueber ::1; Abbruch")
        print(f"trace-proxy: kein ::1 verfuegbar ({exc}); nur 127.0.0.1", flush=True)
    if v6 is not None:
        threading.Thread(target=v6.serve_forever, daemon=True).start()
    v4.serve_forever()   # blockiert im Hauptthread


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default=UPSTREAM); ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--dialect", choices=("openai", "anthropic", "codex"), default="openai")
    ap.add_argument("--project", default="arena-proxy"); ap.add_argument("--phoenix", default="http://localhost:6006")
    ap.add_argument("--row", default=""); ap.add_argument("--run-id", default=""); ap.add_argument("--stats-dir", default=None)
    a = ap.parse_args()
    UPSTREAM = a.upstream.rstrip("/"); DIALECT = a.dialect
    Handler.project, Handler.phoenix = a.project, a.phoenix
    STATE.update(row=a.row, run_id=a.run_id, stats_dir=a.stats_dir)
    if a.stats_dir: os.makedirs(a.stats_dir, exist_ok=True)
    print(f"trace-proxy[{DIALECT}]: 127.0.0.1:{a.port} + [::1]:{a.port} -> {UPSTREAM}  (Projekt {a.project}, Zeile {a.row})", flush=True)
    _serve_dual_stack(a.port, Handler)

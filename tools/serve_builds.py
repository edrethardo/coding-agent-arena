"""Static server for the collected builds, tolerant of the mount prefix.

`tailscale serve --set-path /arena` strips the prefix before proxying, so the backend sees
`/builds/...` for a request to `/arena/builds/...` and cannot tell the two apart -- which
means it cannot redirect `/arena` to `/arena/` either. A page opened without the trailing
slash therefore resolved its relative links against `/`, where a different service is
mounted, and every link on the index was dead. Measured, not theorised: `/arena` returned
200 and its links 302'd into the neighbouring mount.

The index carries `<base href="<mount>/">` so links resolve identically with or without the
slash. That alone would break direct access at 127.0.0.1, so this server also accepts the
prefix and strips it -- both routes work, which keeps the local debug path usable.
"""
import functools
import http.server
import os
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    mount = ""

    def translate_path(self, path):
        if self.mount and (path == self.mount or path.startswith(self.mount + "/")):
            path = path[len(self.mount):] or "/"
        return super().translate_path(path)

    def end_headers(self):
        # Die Uebersichtsseiten aendern sich alle paar Minuten, waehrend Laeufe Runden
        # abliefern. Ohne Cache-Control schickt der Server nur `Last-Modified`, und
        # Browser leiten daraus heuristisch eine Haltbarkeit ab (ueblich: ein Zehntel des
        # Alters) -- gemessen am 2026-09-09: eine frisch veroeffentlichte Zeile war im
        # Browser nicht zu sehen, auf der Platte aber vorhanden. Die Build-Dateien selbst
        # sind je URL unveraenderlich und duerfen zwischengespeichert werden.
        path = self.path.split("?", 1)[0]
        if path.endswith((".html", "/")) and "/builds/" not in path:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass   # the round is the record; request noise is not


def main():
    root, port, mount = sys.argv[1], int(sys.argv[2]), sys.argv[3].rstrip("/")
    Handler.mount = mount
    handler = functools.partial(Handler, directory=root)
    # Bound to loopback: the tailnet proxy is the only way in.
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as srv:
        print(f"serving {root} on 127.0.0.1:{port}, tolerating prefix {mount!r}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()

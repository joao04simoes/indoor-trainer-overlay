"""Apresentação via mpv e HTTP."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def mmss(s):
    if s is None:
        return 'sem limite'
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def power_ok(snap):
    t, p = snap.get("target"), snap.get("power3")
    return t is None or p is None or abs(p - t) <= 0.05 * t + 5


def has_power(trainer, now):
    return (trainer.power is not None and trainer.power > 0
            and trainer.power_at is not None and 0 <= now - trainer.power_at <= 3)


def ass_text(s):
    if s.get("done"):
        return r"{\an7\pos(60,50)\fs70\b1\bord3\1c&HFFFFFF&\3c&H000000&}Treino terminado"
    col = "&H78C850&" if power_ok(s) else "&H1E8CFF&"
    tgt = f" / {s['target']} W" if s.get("target") is not None else ""
    nxt = ""
    if s.get("next_watts") is not None:
        nxt = r"\N{\fs30\1c&HDDDDDD&}Proximo: " + f"{s['next_watts']} W - {mmss(s['next_dur'])}"
    if s.get("paused"):
        nxt += r"\N{\fs30}PAUSA - pedala para continuar"
    hr = f"   FC {s['hr']:.0f}" if s.get("hr") else ""
    cad = f"CAD {s['cadence']:.0f}" if s.get("cadence") is not None else ""
    return (r"{\an7\pos(60,50)\bord3\shad1\b1\1c&HFFFFFF&\3c&H000000&}"
            rf"{{\fs34\1c&HDDDDDD&}}{s['name']}  ({s['idx']}/{s['n']})\N"
            rf"{{\fs100\1c{col}}}{s['power3']:.0f}{{\fs44\1c&HFFFFFF&}} W{tgt}\N"
            rf"{{\fs46\1c&HFFFFFF&}}{mmss(s['remaining'])}   {cad}{hr}   {s['speed']:.1f} km/h"
            rf"{nxt}")


class MpvOverlay:
    def __init__(self):
        self.w = None

    async def connect(self, sock):
        for _ in range(100):
            if os.path.exists(sock):
                break
            await asyncio.sleep(0.1)
        r, self.w = await asyncio.open_unix_connection(sock)
        asyncio.create_task(self._drain(r))

    @staticmethod
    async def _drain(r):
        try:
            while await r.readline():
                pass
        except Exception:
            pass

    async def show(self, snap):
        if not self.w:
            return
        try:
            cmd = {"command": ["osd-overlay", 1, "ass-events", ass_text(snap), 1920, 1080]}
            self.w.write((json.dumps(cmd) + "\n").encode())
            await self.w.drain()
        except Exception:
            self.w = None  # mpv fechado: continua sem overlay


WEB_HTML = """<!doctype html><meta charset=utf-8><title>ergo</title><style>
html,body{margin:0;background:transparent;color:#fff;font-family:Arial,sans-serif;
text-shadow:0 0 6px #000,0 0 3px #000,0 0 3px #000}#b{padding:24px;line-height:1.15}
.big{font-size:100px;font-weight:700}.s{font-size:34px;color:#ddd}.m{font-size:46px}
.ok{color:#50c878}.off{color:#ff8c1e}</style><div id=b></div><script>
const f=s=>{if(s==null)return 'sem limite';s=Math.max(0,s|0);return (s/60|0)+':'+String(s%60).padStart(2,'0')};
async function t(){try{const s=await(await fetch('/state')).json();let h='';
if(s.done)h='<div class=big>Treino terminado</div>';
else if(s.name){h=`<div class=s>${s.paused?'PAUSA - pedala para continuar':s.name} (${s.idx}/${s.n})</div>
<div><span class="big ${s.ok?'ok':'off'}">${s.power3|0}</span><span class=m> W${s.target!=null?' / '+s.target+' W':''}</span></div>
<div class=m>${f(s.remaining)} &nbsp; ${s.cadence!=null?'CAD '+(s.cadence|0):''} &nbsp; ${s.hr?'FC '+(s.hr|0):''} &nbsp; ${s.speed.toFixed(1)} km/h</div>
${s.next_watts!=null?`<div class=s>Proximo: ${s.next_watts} W - ${f(s.next_dur)}</div>`:''}`}
document.getElementById('b').innerHTML=h}catch(e){}setTimeout(t,500)}t()</script>"""


def start_web(state, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/state"):
                body, ct = json.dumps(state["s"]).encode(), "application/json"
            else:
                body, ct = WEB_HTML.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"overlay web: http://127.0.0.1:{port}/  (fonte de navegador no OBS, ou janela do browser)")
    return srv



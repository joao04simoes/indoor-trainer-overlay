#!/usr/bin/env python3
"""ergo.py - corre treinos .fit em modo ERG num rolo FTMS (ThinkRider), grava o
treino em .fit (para o Strava) e mostra um overlay por cima de um filme.

Dependencias: Python 3.10+, bleak (pip install bleak), mpv (opcional, para o overlay).

Exemplos:
  python ergo.py --make-demo demo.fit
  python ergo.py demo.fit --sim --film filme.mkv --web        # teste sem rolo
  python ergo.py treino.fit --ftp 230 --film filme.mkv --fs   # treino a serio
  python ergo.py --scan                                       # listar dispositivos BLE
  python ergo.py treino.fit --hr --overlay                    # por cima do Stremio

O tempo do treino e a gravacao FIT apenas avancam com potencia > 0 W recebida
nos ultimos 3 segundos. As pausas ficam excluidas do tempo gravado.
--overlay requer PyQt5. Arrasta o texto
para a mover; fechar a janela nao termina o treino (Ctrl+C no terminal termina).
Overlay sem moldura e sem fundo: botao direito ou Esc fecha.
"""
import argparse
import asyncio
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# ============================================================================
# FIT: leitura (treinos) e escrita (atividade / treino demo)
# ============================================================================
FIT_EPOCH = 631065600  # 1989-12-31 00:00:00 UTC em segundos unix
_CRC_T = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
          0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def crc16(data: bytes, crc: int = 0) -> int:
    for b in data:
        t = _CRC_T[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ t ^ _CRC_T[b & 0xF]
        t = _CRC_T[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ t ^ _CRC_T[(b >> 4) & 0xF]
    return crc


# tipo base -> (formato struct, tamanho)
_BT = {0: ("B", 1), 1: ("b", 1), 2: ("B", 1), 3: ("h", 2), 4: ("H", 2), 5: ("i", 4),
       6: ("I", 4), 10: ("B", 1), 11: ("H", 2), 12: ("I", 4), 13: ("B", 1)}


def _invalid(bt: int, v: int) -> bool:
    return ((bt in (0, 2, 13) and v == 0xFF) or (bt == 4 and v == 0xFFFF)
            or (bt == 6 and v == 0xFFFFFFFF) or (bt == 1 and v == 0x7F)
            or (bt == 3 and v == 0x7FFF) or (bt == 5 and v == 0x7FFFFFFF)
            or (bt in (10, 11, 12) and v == 0))


def decode_fit(data: bytes) -> dict:
    """Devolve {numero_global_mensagem: [ {numero_campo: valor}, ... ]}."""
    if len(data) < 14 or data[8:12] != b".FIT":
        raise ValueError("isto nao parece um ficheiro .fit")
    hs = data[0]
    size = struct.unpack_from("<I", data, 4)[0]
    pos, end = hs, min(hs + size, len(data))
    defs, out = {}, {}
    while pos < end:
        h = data[pos]
        pos += 1
        if not (h & 0x80) and (h & 0x40):  # definicao
            endian = ">" if data[pos + 1] else "<"
            gnum, n = struct.unpack_from(endian + "HB", data, pos + 2)
            pos += 5
            fields = [tuple(data[pos + 3 * i: pos + 3 * i + 3]) for i in range(n)]
            pos += 3 * n
            dev = 0
            if h & 0x20:
                m = data[pos]
                pos += 1
                for i in range(m):
                    dev += data[pos + 3 * i + 1]
                pos += 3 * m
            defs[h & 0x0F] = (endian, gnum, fields, dev)
            continue
        local = (h >> 5) & 3 if h & 0x80 else h & 0x0F  # timestamp comprimido ou normal
        endian, gnum, fields, dev = defs[local]
        rec = {}
        for num, sz, bt in fields:
            raw = data[pos:pos + sz]
            pos += sz
            btn = bt & 0x1F
            if btn == 7:
                rec[num] = raw.split(b"\0")[0].decode("utf-8", "replace")
            elif btn in _BT and len(raw) >= _BT[btn][1]:
                f, s = _BT[btn]
                v = struct.unpack(endian + f, raw[:s])[0]
                rec[num] = None if _invalid(btn, v) else v
        pos += dev
        out.setdefault(gnum, []).append(rec)
    return out


_SZ = {0: 1, 2: 1, 4: 2, 6: 4, 7: 16}
_BTB = {0: 0x00, 2: 0x02, 4: 0x84, 6: 0x86, 7: 0x07}
_INV = {0: 0xFF, 2: 0xFF, 4: 0xFFFF, 6: 0xFFFFFFFF}
_FMT = {0: "B", 2: "B", 4: "H", 6: "I"}


class FitWriter:
    """Escritor minimo: msg(num_global, [(campo, tipo_base, valor), ...])."""

    def __init__(self):
        self.body = bytearray()
        self.local = {}

    def msg(self, gnum, fields):
        sig = (gnum, tuple((n, bt) for n, bt, _ in fields))
        if sig not in self.local:
            lid = len(self.local)
            if lid > 15:
                raise RuntimeError("demasiados tipos de mensagem")
            self.local[sig] = lid
            self.body += bytes([0x40 | lid, 0, 0]) + struct.pack("<HB", gnum, len(fields))
            self.body += b"".join(bytes((n, _SZ[bt], _BTB[bt])) for n, bt, _ in fields)
        self.body.append(self.local[sig])
        for n, bt, v in fields:
            if bt == 7:
                self.body += str(v).encode()[:15].ljust(16, b"\0")
            else:
                v = _INV[bt] if v is None else int(round(v))
                self.body += struct.pack("<" + _FMT[bt], max(0, min(v, _INV[bt])))

    def save(self, path):
        hdr = struct.pack("<BBHI4s", 14, 0x20, 2140, len(self.body), b".FIT")
        hdr += struct.pack("<H", crc16(hdr))
        blob = hdr + bytes(self.body)
        Path(path).write_bytes(blob + struct.pack("<H", crc16(blob)))


def write_activity(path, samples, start_unix):
    """samples: lista de dicts com power, cadence, speed (km/h), hr, dist (m)."""
    w = FitWriter()
    t0 = int(start_unix) - FIT_EPOCH
    n = len(samples)
    t1 = t0 + n
    w.msg(0, [(0, 0, 4), (1, 4, 255), (2, 4, 0), (4, 6, t0)])
    w.msg(21, [(253, 6, t0), (0, 0, 0), (1, 0, 0)])
    for i, s in enumerate(samples):
        w.msg(20, [(253, 6, t0 + i), (3, 2, s.get("hr")),
                   (4, 2, s.get("cadence")), (5, 6, s["dist"] * 100),
                   (6, 4, s["speed"] / 3.6 * 1000), (7, 4, s["power"])])
    w.msg(21, [(253, 6, t1), (0, 0, 0), (1, 0, 4)])
    pw = [s["power"] for s in samples] or [0]
    hrs = [s["hr"] for s in samples if s.get("hr")]
    cad = [s["cadence"] for s in samples if s.get("cadence")]
    dist = samples[-1]["dist"] if samples else 0
    tot = [(7, 6, n * 1000), (8, 6, n * 1000), (9, 6, dist * 100)]
    w.msg(19, [(253, 6, t1), (254, 4, 0), (0, 0, 9), (1, 0, 1), (2, 6, t0)] + tot)
    w.msg(18, [(253, 6, t1), (254, 4, 0), (0, 0, 8), (1, 0, 1), (2, 6, t0),
               (5, 0, 2), (6, 0, 6)] + tot +
          [(14, 4, (dist / n * 1000) if n else 0), (16, 2, sum(hrs) / len(hrs) if hrs else None),
           (17, 2, max(hrs) if hrs else None), (18, 2, sum(cad) / len(cad) if cad else None),
           (20, 4, sum(pw) / len(pw)), (21, 4, max(pw)), (25, 4, 0), (26, 4, 1)])
    w.msg(34, [(253, 6, t1), (0, 6, n * 1000), (1, 4, 1), (2, 0, 0), (3, 0, 26), (4, 0, 1)])
    w.save(path)


def write_demo_workout(path):
    """Treino de teste: aquecimento, 3x(3' forte / 2' leve), arrefecimento. Em % FTP."""
    steps = [  # (nome, duracao_s, %ftp_baixo, %ftp_alto, intensidade) ou ("repeat", de_passo, vezes)
        ("Aquecimento", 300, 50, 60, 2),
        ("Forte", 180, 95, 95, 0),
        ("Leve", 120, 55, 55, 1),
        ("repeat", 1, 3),
        ("Arrefecer", 300, 50, 40, 3),
    ]
    w = FitWriter()
    w.msg(0, [(0, 0, 5), (1, 4, 255), (2, 4, 0), (4, 6, int(time.time()) - FIT_EPOCH)])
    w.msg(26, [(4, 0, 2), (6, 4, len(steps)), (8, 7, "Demo 3x3")])
    for i, st in enumerate(steps):
        if st[0] == "repeat":
            w.msg(27, [(254, 4, i), (0, 7, ""), (1, 0, 6), (2, 6, st[1]), (3, 0, 2),
                       (4, 6, st[2]), (5, 6, None), (6, 6, None), (7, 0, 0)])
        else:
            nm, d, lo, hi, it = st
            w.msg(27, [(254, 4, i), (0, 7, nm), (1, 0, 0), (2, 6, d * 1000), (3, 0, 4),
                       (4, 6, 0), (5, 6, lo), (6, 6, hi), (7, 0, it)])
    w.save(path)


# ============================================================================
# Treino: passos -> blocos (duracao, watts)
# ============================================================================
@dataclass
class Block:
    dur: int
    watts: Optional[int]
    name: str


_ZONE_PCT = {1: 0.5, 2: 0.65, 3: 0.83, 4: 0.98, 5: 1.13, 6: 1.35, 7: 1.6}
_INTENS = {0: "Ativo", 1: "Descanso", 2: "Aquecimento", 3: "Arrefecimento", 4: "Recuperacao"}


def load_workout(path, ftp):
    msgs = decode_fit(Path(path).read_bytes())
    name = (msgs.get(26) or [{}])[0].get(8) or Path(path).stem
    raw = msgs.get(27)
    if not raw:
        raise ValueError("este .fit nao tem passos de treino (workout_step)")
    raw = sorted(enumerate(raw), key=lambda x: x[1].get(254, x[0]))
    steps = [r for _, r in raw]
    uses_pct = False

    def watts(r):
        nonlocal uses_pct
        if r.get(3) != 4:
            return None  # sem alvo de potencia
        if r.get(4):  # zona de potencia
            uses_pct = True
            return ftp * _ZONE_PCT.get(r[4], 1.0)
        vals = []
        for v in (r.get(5), r.get(6)):
            if v is None:
                continue
            if v > 1000:
                vals.append(v - 1000)
            else:
                uses_pct = True
                vals.append(v * ftp / 100)
        return sum(vals) / len(vals) if vals else None

    blocks, i, done = [], 0, {}
    while i < len(steps):
        r = steps[i]
        dt = r.get(1)
        if dt == 6:  # repetir passos
            done[i] = done.get(i, 0) + 1
            if done[i] < (r.get(4) or 1):
                for k in range(r.get(2) or 0, i):
                    done.pop(k, None)
                i = r.get(2) or 0
                continue
            done.pop(i, None)
        elif dt == 0 and r.get(2):
            nm = r.get(0) or _INTENS.get(r.get(7), f"Passo {i + 1}")
            w = watts(r)
            blocks.append(Block(int(r[2] / 1000), None if w is None else int(round(w)), nm))
        else:
            print(f"aviso: passo {i + 1} ignorado (tipo de duracao {dt} nao suportado)")
        i += 1
    if not blocks:
        raise ValueError("nenhum passo com duracao em tempo")
    return name, blocks, uses_pct


# ============================================================================
# Bluetooth: rolo FTMS e cinta cardiaca
# ============================================================================
FTMS = "00001826-0000-1000-8000-00805f9b34fb"
IBD = "00002ad2-0000-1000-8000-00805f9b34fb"
CP = "00002ad9-0000-1000-8000-00805f9b34fb"
HRS = "0000180d-0000-1000-8000-00805f9b34fb"
HRM = "00002a37-0000-1000-8000-00805f9b34fb"


async def find_device(service, name=None, address=None, timeout=12):
    from bleak import BleakScanner
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    def ok(d, ad):
        return (service in [u.lower() for u in ad.service_uuids]
                and (not name or name.lower() in (d.name or "").lower()))

    return await BleakScanner.find_device_by_filter(ok, timeout=timeout)


class FTMSTrainer:
    def __init__(self, name=None, address=None, on_lost=None):
        self.name, self.address, self.on_lost = name, address, on_lost
        self.power = self.cadence = self.speed = self.hr = None
        self.client = None
        self._fut = None
        self._ready = False
        self.power_at = None

    def _on_disconnect(self, client):
        if self._ready and client is self.client:
            self._ready = False
            if self.on_lost:
                self.on_lost()

    async def connect(self):
        from bleak import BleakClient
        from bleak.exc import BleakError
        for attempt in range(1, 4):
            try:
                await self._connect_once(BleakClient)
                self._ready = True
                return
            except (BleakError, asyncio.TimeoutError) as exc:
                await self.close()
                if attempt == 3:
                    raise RuntimeError(
                        "nao foi possivel ligar ao rolo apos 3 tentativas. "
                        "Fecha MyWhoosh/Zwift e apps no telemovel, desliga e volta "
                        "a ligar o rolo e tenta novamente. Detalhe: " + str(exc)
                    ) from exc
                print(f"aviso: ligacao BLE falhou ({attempt}/3): {exc}; a tentar novamente...")
                await asyncio.sleep(2)

    async def _connect_once(self, client_class):
        print("a procurar o rolo (FTMS)...")
        dev = await find_device(FTMS, self.name, self.address)
        if not dev:
            raise RuntimeError("rolo nao encontrado: ligado? MyWhoosh/telemovel fechados? usa --scan")
        print(f"a ligar a {dev.name} [{dev.address}]")
        self.client = client_class(dev, disconnected_callback=self._on_disconnect, timeout=30)
        await self.client.connect()
        await self.client.start_notify(CP, self._on_cp)
        await self.client.start_notify(IBD, self._on_data)
        r = await self._cp(b"\x00")  # pedir controlo
        if r != (0x00, 0x01):
            print(f"aviso: resposta ao pedido de controlo = {r}")
        await self._cp(b"\x07")  # iniciar/retomar

    def _on_cp(self, _, d):
        if len(d) >= 3 and d[0] == 0x80 and self._fut and not self._fut.done():
            self._fut.set_result((d[1], d[2]))

    async def _cp(self, payload):
        self._fut = asyncio.get_running_loop().create_future()
        await self.client.write_gatt_char(CP, payload, response=True)
        try:
            return await asyncio.wait_for(self._fut, 3)
        except asyncio.TimeoutError:
            return None

    def _on_data(self, _, d):
        f = struct.unpack_from("<H", d, 0)[0]
        p = 2

        def take(fmt):
            nonlocal p
            v = struct.unpack_from("<" + fmt, d, p)[0]
            p += struct.calcsize(fmt)
            return v

        if not f & 1:
            self.speed = take("H") / 100
        if f & 2:
            take("H")
        if f & 4:
            self.cadence = take("H") / 2
        if f & 8:
            take("H")
        if f & 16:
            p += 3
        if f & 32:
            take("h")
        if f & 64:
            self.power = take("h")
            self.power_at = time.monotonic()
        if f & 128:
            take("h")
        if f & 256:
            p += 5
        if f & 512:
            self.hr = take("B") or None

    async def set_power(self, watts):
        r = await self._cp(struct.pack("<Bh", 0x05, int(watts)))
        if r and r[1] != 0x01:
            print(f"aviso: o rolo recusou {watts} W (resposta {r})")

    async def close(self):
        was_ready, self._ready = self._ready, False
        try:
            if self.client and self.client.is_connected:
                try:
                    if was_ready:
                        # Retirar o alvo ERG antes de parar e repor os valores por defeito.
                        # Tentar cada passo mesmo quando o firmware rejeita o anterior.
                        for payload in (b"\x05\x00\x00", b"\x08\x01", b"\x01"):
                            try:
                                response = await self._cp(payload)
                                if response != (payload[0], 0x01):
                                    print(f"aviso: ao libertar o rolo, comando {payload.hex()}: {response}")
                            except Exception as exc:
                                print(f"aviso: ao libertar o rolo: {exc}")
                finally:
                    await self.client.disconnect()
        except Exception as e:
            print("aviso ao desligar o rolo:", e)


class SimTrainer:
    """Rolo falso para testar tudo sem hardware."""

    def __init__(self):
        self.target, self.power, self.cadence, self.speed, self.hr = 100, 0.0, 90, 25.0, 100
        self.power_at = None

    async def connect(self):
        print("[sim] rolo simulado")

    async def set_power(self, watts):
        self.target = watts

    def tick(self, t):
        import math
        self.power += (self.target - self.power) * 0.4
        self.cadence = 88 + 3 * math.sin(t / 7)
        self.speed = 15 + self.power * 0.05
        self.hr = 110 + self.power * 0.2
        self.power_at = time.monotonic()

    async def close(self):
        pass


class HRSensor:
    def __init__(self, name=None):
        self.name, self.hr, self.client = name, None, None

    async def connect(self):
        from bleak import BleakClient
        dev = await find_device(HRS, self.name or None)
        if not dev:
            print("aviso: cinta cardiaca nao encontrada, continuo sem ela")
            return
        self.client = BleakClient(dev)
        await self.client.connect()
        await self.client.start_notify(HRM, self._on)
        print(f"cinta cardiaca: {dev.name}")

    def _on(self, _, d):
        self.hr = struct.unpack_from("<H", d, 1)[0] if d[0] & 1 else d[1]

    async def close(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()


# ============================================================================
# Overlays: mpv (ASS por IPC) e pagina web (OBS / janela transparente)
# ============================================================================
def mmss(s):
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


def power_ok(snap):
    t, p = snap.get("target"), snap.get("power3")
    return t is None or p is None or abs(p - t) <= 0.05 * t + 5


def has_power(trainer, now):
    return (trainer.power is not None and trainer.power > 0
            and trainer.power_at is not None and 0 <= now - trainer.power_at <= 3)


class DesktopOverlay:
    """Janela independente, movel e sempre por cima do leitor de video."""

    def __init__(self):
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
        except ImportError:
            # Reutilizar o PyQt5 do Ubuntu quando o venv nao inclui pacotes do sistema.
            system_packages = "/usr/lib/python3/dist-packages"
            if sys.platform.startswith("linux") and Path(system_packages).is_dir():
                if system_packages not in sys.path:
                    sys.path.append(system_packages)
            try:
                from PyQt5 import QtCore, QtGui, QtWidgets
            except ImportError as exc:
                raise RuntimeError("o overlay transparente requer PyQt5: pip install PyQt5") from exc

        class TextWindow(QtWidgets.QWidget):
            def __init__(window):
                super().__init__()
                window.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint
                                      | QtCore.Qt.Tool | QtCore.Qt.NoDropShadowWindowHint)
                window.setAttribute(QtCore.Qt.WA_TranslucentBackground)
                window.setWindowTitle("ergo — overlay")
                window.setGeometry(40, 40, 700, 135)
                window.text = "A ligar ao rolo..."
                window.offset = None

            def paintEvent(window, event):
                painter = QtGui.QPainter(window)
                painter.setRenderHint(QtGui.QPainter.Antialiasing)
                font = QtGui.QFont("Sans", 17, QtGui.QFont.Bold)
                metrics = QtGui.QFontMetrics(font)
                path = QtGui.QPainterPath()
                for i, line in enumerate(window.text.splitlines()):
                    path.addText(10, 10 + metrics.ascent() + i * metrics.lineSpacing(), font, line)
                # Contorno primeiro; preencher depois preserva o interior das letras.
                painter.strokePath(path, QtGui.QPen(QtCore.Qt.black, 2.5, QtCore.Qt.SolidLine,
                                                   QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
                painter.fillPath(path, QtGui.QColor("#FF9F1C"))

            def mousePressEvent(window, event):
                if event.button() == QtCore.Qt.RightButton:
                    self.close()
                elif event.button() == QtCore.Qt.LeftButton:
                    window.offset = event.globalPos() - window.pos()

            def mouseMoveEvent(window, event):
                if window.offset is not None and event.buttons() & QtCore.Qt.LeftButton:
                    window.move(event.globalPos() - window.offset)

            def keyPressEvent(window, event):
                if event.key() == QtCore.Qt.Key_Escape:
                    self.close()

        try:
            self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["ergo-overlay"])
            self.app.setQuitOnLastWindowClosed(False)
            self.root = TextWindow()
            self.root.show()
            self.pump()
        except Exception as exc:
            raise RuntimeError(
                "nao foi possivel abrir o overlay transparente: " + str(exc)
            ) from exc

    def show(self, s):
        if self.root is None:
            return
        if s.get("done"):
            text = "Treino terminado"
        else:
            status = "PAUSA — pedala para continuar" if s["paused"] else s["name"]
            text = (f"{status} ({s['idx']}/{s['n']})\n"
                    f"{s['power3']:.0f} W / {s['target']} W     {mmss(s['remaining'])}\n"
                    f"CAD {s['cadence'] or 0:.0f}    FC {s['hr'] or 0:.0f}    "
                    f"Total {mmss(s['elapsed'])} / {mmss(s['total'])}")
        self.root.text = text
        self.root.update()
        self.pump()

    def close(self):
        if self.root is not None:
            self.root.close()
            self.root = None

    def pump(self):
        if self.root is not None:
            self.app.processEvents()


def ass_text(s):
    if s.get("done"):
        return r"{\an7\pos(60,50)\fs70\b1\bord3\1c&HFFFFFF&\3c&H000000&}Treino terminado"
    col = "&H78C850&" if power_ok(s) else "&H1E8CFF&"
    tgt = f" / {s['target']} W" if s.get("target") is not None else ""
    nxt = ""
    if s.get("next_watts") is not None:
        nxt = rf"\N{{\fs30\1c&HDDDDDD&}}Proximo: {s['next_watts']} W - {mmss(s['next_dur'])}"
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
const f=s=>{s=Math.max(0,s|0);return (s/60|0)+':'+String(s%60).padStart(2,'0')};
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


# ============================================================================
# Execucao do treino
# ============================================================================
async def run(args):
    name, blocks, uses_pct = load_workout(args.workout, args.ftp)
    total = sum(b.dur for b in blocks)
    print(f"treino: {name} | {len(blocks)} blocos | {mmss(total)}"
          + (f" | FTP assumido: {args.ftp} W" if uses_pct else ""))
    for i, b in enumerate(blocks, 1):
        print(f"  {i:2d}. {mmss(b.dur):>6}  {('%d W' % b.watts) if b.watts is not None else 'livre':>7}  {b.name}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sg in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sg, stop.set)

    trainer = SimTrainer() if args.sim else FTMSTrainer(args.trainer, args.address, on_lost=lambda: (
        print("\naviso: rolo desligou-se, a guardar o que existe"), stop.set()))
    hr_sensor = HRSensor(args.hr) if args.hr is not None and not args.sim else None
    state = {"s": {}}
    desktop = DesktopOverlay() if args.overlay else None
    mpv_proc, mpv = None, None
    sock = args.mpv_socket
    if args.film:
        sock = sock or "/tmp/ergo-mpv.sock"
        if os.path.exists(sock):
            os.remove(sock)
        cmd = ["mpv", f"--input-ipc-server={sock}", "--pause", "--hwdec=auto"]
        if args.fs:
            cmd.append("--fs")
        mpv_proc = subprocess.Popen(cmd + [args.film])
    if args.web:
        start_web(state, args.port)

    try:
        await trainer.connect()
    except BaseException:
        if desktop:
            desktop.close()
        raise
    if hr_sensor:
        await hr_sensor.connect()
    if sock:
        mpv = MpvOverlay()
        try:
            await mpv.connect(sock)
        except Exception as e:
            print("aviso: sem overlay mpv:", e)
            mpv = None

    if not args.yes:
        await loop.run_in_executor(None, input, "Pronto. Prime Enter para comecar (e carrega play no filme)... ")

    samples, dist, cur = [], 0.0, -1
    start_unix = None
    recent = []
    bounds = [sum(b.dur for b in blocks[:i + 1]) for i in range(len(blocks))]
    target = 100
    try:
        while not stop.is_set():
            el = len(samples)
            if el >= total:
                break
            idx = next(i for i, e in enumerate(bounds) if el < e)
            b = blocks[idx]
            if idx != cur:
                cur = idx
                if b.watts is not None:
                    target = max(0, min(2000, b.watts))
                await trainer.set_power(target)
            if isinstance(trainer, SimTrainer):
                trainer.tick(el)
            active = has_power(trainer, time.monotonic())
            p = trainer.power if active else 0
            speed = trainer.speed or 0
            hr = (hr_sensor.hr if hr_sensor and hr_sensor.hr else trainer.hr)
            if active:
                if start_unix is None:
                    start_unix = time.time()
                dist += speed / 3.6
                samples.append({"power": max(0, p), "cadence": trainer.cadence, "speed": speed,
                                "hr": hr, "dist": dist})
            recent = (recent + [p])[-3:]
            nb = blocks[idx + 1] if idx + 1 < len(blocks) else None
            snap = {"name": b.name, "idx": idx + 1, "n": len(blocks), "power": p,
                    "power3": sum(recent) / len(recent), "target": b.watts if b.watts is not None else target,
                    "cadence": trainer.cadence, "hr": hr, "speed": speed,
                    "remaining": bounds[idx] - el, "elapsed": el, "total": total, "paused": not active,
                    "next_watts": nb.watts if nb else None, "next_dur": nb.dur if nb else 0, "done": False}
            snap["ok"] = power_ok(snap)
            state["s"] = snap
            if desktop:
                desktop.show(snap)
            if mpv:
                await mpv.show(snap)
            print(f"\r{mmss(el)}/{mmss(total)}  alvo {snap['target']:>4} W  potencia {p:>4.0f} W  "
                  f"cad {trainer.cadence or 0:>3.0f}  {'PAUSA - sem watts' if not active else '                  '}", end="", flush=True)
            # Sem compensar atrasos com amostras instantaneas; mantem a janela responsiva.
            deadline = time.monotonic() + 1
            while not stop.is_set() and time.monotonic() < deadline:
                if desktop:
                    desktop.pump()
                try:
                    await asyncio.wait_for(stop.wait(), min(0.05, max(0, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    pass
    finally:
        print()
        state["s"] = {"done": True}
        if desktop:
            desktop.close()
        if mpv:
            await mpv.show({"done": True})
        await trainer.close()
        if hr_sensor:
            await hr_sensor.close()
        if len(samples) >= 5:
            out = Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            f = out / datetime.fromtimestamp(start_unix).strftime("ergo_%Y%m%d_%H%M.fit")
            write_activity(f, samples, start_unix)
            print(f"treino guardado: {f}\nCarrega-o em strava.com/upload/select")
        else:
            print("treino demasiado curto, nada guardado")


def main():
    ap = argparse.ArgumentParser(description="Treinos .fit em ERG + gravacao .fit + overlay")
    ap.add_argument("workout", nargs="?", help="ficheiro .fit de treino")
    ap.add_argument("--ftp", type=int, default=200, help="FTP em W (para passos em %% FTP)")
    ap.add_argument("--trainer", help="parte do nome BLE do rolo")
    ap.add_argument("--address", help="endereco BLE do rolo")
    ap.add_argument("--hr", nargs="?", const="", help="usar cinta cardiaca BLE (opcional: parte do nome)")
    ap.add_argument("--film", help="abre este filme no mpv com overlay")
    ap.add_argument("--fs", action="store_true", help="mpv em ecra inteiro")
    ap.add_argument("--mpv-socket", help="ligar a um mpv ja aberto (--input-ipc-server=...)")
    ap.add_argument("--web", action="store_true", help="overlay web em localhost")
    ap.add_argument("--overlay", action="store_true", help="janela sempre por cima (Stremio ou outro leitor)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--out", default="~/rides", help="pasta dos .fit gravados")
    ap.add_argument("--sim", action="store_true", help="rolo simulado (teste)")
    ap.add_argument("--yes", action="store_true", help="nao esperar pelo Enter")
    ap.add_argument("--scan", action="store_true", help="listar dispositivos BLE")
    ap.add_argument("--make-demo", metavar="FICHEIRO", help="criar um treino .fit de exemplo")
    a = ap.parse_args()
    if a.make_demo:
        write_demo_workout(a.make_demo)
        print("criado", a.make_demo)
    elif a.scan:
        async def scan():
            from bleak import BleakScanner
            for d, ad in (await BleakScanner.discover(timeout=8, return_adv=True)).values():
                tag = " <- FTMS" if FTMS in [u.lower() for u in ad.service_uuids] else ""
                print(f"{d.address}  {d.name or '?'}{tag}")
        asyncio.run(scan())
    elif a.workout:
        try:
            asyncio.run(run(a))
        except RuntimeError as exc:
            print(f"erro: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

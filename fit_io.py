"""Leitura e gravação de FIT."""
import struct
import time
from pathlib import Path

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



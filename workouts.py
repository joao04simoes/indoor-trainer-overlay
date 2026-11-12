"""Blocos de treino."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from fit_io import decode_fit

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



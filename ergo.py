#!/usr/bin/env python3
"""Arranque e execução de sessões de ciclismo."""
import argparse
import asyncio
import math
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from fit_io import decode_fit, write_activity, write_demo_workout, FitWriter
from workouts import Block, load_workout
from devices import FTMS, FTMSTrainer, SimTrainer, HRSensor
from overlays import mmss, power_ok, has_power, MpvOverlay, start_web
from desktop import DesktopOverlay, setup_session

# ============================================================================
# Execucao do treino
# ============================================================================
async def run(args):
    if args.workout:
        name, blocks, uses_pct = load_workout(args.workout, args.ftp)
        total = sum(b.dur for b in blocks)
    else:
        watts = getattr(args, 'power', None)
        duration = getattr(args, 'duration', None)
        total = int(duration * 60) if duration else None
        name = 'Potência constante' if watts is not None else 'Treino livre'
        blocks, uses_pct = [Block(total or 2**32, watts, name)], False
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
    controls = desktop.controls if desktop else {'paused': False, 'stop': False, 'delta': 0}
    async def pump_ui():
        while True:
            if desktop:
                desktop.pump()
            await asyncio.sleep(.025)
    ui_task = asyncio.create_task(pump_ui())
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
        ui_task.cancel()
        await trainer.close()
        if desktop:
            desktop.close()
        raise
    if hr_sensor:
        try:
            await hr_sensor.connect()
        except Exception as exc:
            print('aviso: não foi possível ligar à cinta:', exc)
            await hr_sensor.close()
            hr_sensor = None
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
    profile = [{'duration': block.dur, 'watts': block.watts} for block in blocks] if args.workout else []
    bounds = [sum(b.dur for b in blocks[:i + 1]) for i in range(len(blocks))]
    target = None
    manual_pause = False
    try:
        while not stop.is_set() and not controls['stop']:
            el = len(samples)
            if total is not None and el >= total:
                break
            idx = next(i for i, e in enumerate(bounds) if el < e)
            b = blocks[idx]
            if idx != cur:
                cur = idx
                previous_target = target
                target = max(0, min(2000, b.watts)) if b.watts is not None else None
                if target is not None:
                    await trainer.set_power(target)
                elif previous_target is not None:
                    await trainer.release_power()
            if controls['delta']:
                target = max(0, min(2000, (target if target is not None else trainer.power or 100) + controls['delta']))
                controls['delta'] = 0
                if not controls['paused']:
                    await trainer.set_power(target)
            if manual_pause != controls['paused']:
                manual_pause = controls['paused']
                if target is not None:
                    await trainer.set_power(0 if manual_pause else target)
            if isinstance(trainer, SimTrainer):
                trainer.tick(el)
            active = has_power(trainer, time.monotonic()) and not manual_pause
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
                    "profile": profile,
                    "power3": sum(recent) / len(recent), "target": target,
                    "cadence": trainer.cadence, "hr": hr, "speed": speed,
                    "remaining": bounds[idx] - el if total is not None else None, "elapsed": el, "total": total, "paused": not active,
                    "dist": dist, "next_name": nb.name if nb else None,
                    "next_watts": nb.watts if nb else None, "next_dur": nb.dur if nb else 0, "done": False}
            snap["ok"] = power_ok(snap)
            state["s"] = snap
            if desktop:
                desktop.show(snap)
            if mpv:
                await mpv.show(snap)
            print(f"\r{mmss(el)}/{mmss(total)}  alvo {str(snap['target'] if snap['target'] is not None else 'livre'):>4} W  potencia {p:>4.0f} W  "
                  f"cad {trainer.cadence or 0:>3.0f}  {'PAUSA - sem watts' if not active else '                  '}", end="", flush=True)
            # Sem compensar atrasos com amostras instantaneas; mantem a janela responsiva.
            deadline = time.monotonic() + 1
            while not stop.is_set() and not controls['stop'] and time.monotonic() < deadline:
                if desktop:
                    desktop.pump()
                try:
                    await asyncio.wait_for(stop.wait(), min(0.05, max(0, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    pass
    finally:
        ui_task.cancel()
        print()
        state["s"] = {"done": True}
        if desktop:
            desktop.close()
        if mpv:
            await mpv.show({"done": True})
        await trainer.close()
        if hr_sensor:
            await hr_sensor.close()
        if samples:
            out = Path(args.out).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            f = out / datetime.fromtimestamp(start_unix).strftime("ergo_%Y%m%d_%H%M%S.fit")
            write_activity(f, samples, start_unix)
            print(f"treino guardado: {f}\nCarrega-o em strava.com/upload/select")
        else:
            print("treino demasiado curto, nada guardado")


def main():
    ap = argparse.ArgumentParser(description="Treinos .fit em ERG + gravacao .fit + overlay")
    ap.add_argument("workout", nargs="?", help="ficheiro .fit de treino")
    ap.add_argument("--ftp", type=int, default=200, help="FTP em W (para passos em %% FTP)")
    ap.add_argument('--power', type=int, help='objetivo constante em W (opcional)')
    ap.add_argument('--duration', type=float, help='duração em minutos (opcional)')
    ap.add_argument('--gui', action='store_true', help='abrir janela de preparação')
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
    if a.power is not None and not 0 <= a.power <= 2000:
        ap.error('--power deve estar entre 0 e 2000 W')
    if a.duration is not None and (not math.isfinite(a.duration) or a.duration < 1 / 60 or a.duration > 1440):
        ap.error('--duration deve estar entre 1 segundo e 1440 minutos')
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
    else:
        try:
            if a.gui or (not a.workout and a.power is None and a.duration is None):
                a = setup_session(a)
                if a is None:
                    return
            asyncio.run(run(a))
        except RuntimeError as exc:
            print(f"erro: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

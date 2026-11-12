"""Testes dos controlos de sessão sem dispositivos físicos."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ergo
from desktop import power_color


class SessionTests(unittest.TestCase):
    def test_power_feedback(self):
        self.assertEqual(power_color(200, None), '#ff9f1c')
        self.assertEqual(power_color(200, 200), '#ff9f1c')
        self.assertEqual(power_color(225, 200), '#ffbd45')
        self.assertEqual(power_color(260, 200), '#ff6262')

    def test_free_and_constant_modes(self):
        for watts in (None, 180):
            with self.subTest(watts=watts), tempfile.TemporaryDirectory() as folder:
                args = SimpleNamespace(workout=None, power=watts, duration=1 / 60,
                                       ftp=200, sim=True, trainer=None, address=None,
                                       hr=None, overlay=False, film=None, web=False,
                                       mpv_socket=None, yes=True, out=folder)
                with patch('builtins.print'):
                    asyncio.run(ergo.run(args))
                files = list(Path(folder).glob('*.fit'))
                self.assertEqual(len(files), 1)
                self.assertEqual(len(ergo.decode_fit(files[0].read_bytes())[20]), 1)

    def test_pause_adjust_resume_and_save_unlimited_session(self):
        controls = {'paused': False, 'stop': False, 'delta': 0}
        targets = []
        snapshots = []

        class Trainer(ergo.SimTrainer):
            async def set_power(self, watts):
                targets.append(watts)
                await super().set_power(watts)

        class Overlay:
            def __init__(self):
                self.controls = controls

            def pump(self):
                pass

            def close(self):
                pass

            def show(self, snap):
                snapshots.append(snap)
                if len(snapshots) == 1:
                    controls['paused'] = True
                elif len(snapshots) == 2:
                    controls.update(paused=False, delta=5)
                else:
                    controls['stop'] = True

        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(workout=None, power=150, duration=None,
                                   ftp=200, sim=True, trainer=None, address=None,
                                   hr=None, overlay=True, film=None, web=False,
                                   mpv_socket=None, yes=True, out=folder)
            with patch.object(ergo, 'DesktopOverlay', Overlay), patch.object(ergo, 'SimTrainer', Trainer), patch('builtins.print'):
                asyncio.run(ergo.run(args))
            self.assertTrue(snapshots[1]['paused'])
            self.assertEqual(snapshots[1]['elapsed'], snapshots[2]['elapsed'])
            self.assertEqual(snapshots[2]['target'], 155)
            self.assertIn(0, targets)
            records = ergo.decode_fit(next(Path(folder).glob('*.fit')).read_bytes())[20]
            self.assertEqual(len(records), 2)


if __name__ == '__main__':
    unittest.main()

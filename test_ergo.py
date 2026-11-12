import asyncio
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ergo


class TrainerReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_releases_erg_before_disconnecting_even_if_stop_fails(self):
        trainer = ergo.FTMSTrainer(on_lost=lambda: self.fail("desligar voluntario"))
        trainer._ready = True
        trainer.client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        commands = []

        async def command(payload):
            self.assertFalse(trainer.client.disconnect.called)
            commands.append(payload)
            if payload == b"\x08\x01":
                raise RuntimeError("stop falhou")
            return payload[0], 1

        trainer._cp = command
        with patch("builtins.print"):
            await trainer.close()
        self.assertEqual(commands, [b"\x05\x00\x00", b"\x08\x01", b"\x01"])
        trainer.client.disconnect.assert_awaited_once()
        trainer._on_disconnect(trainer.client)

    async def test_incomplete_connection_only_disconnects(self):
        trainer = ergo.FTMSTrainer()
        trainer.client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        trainer._cp = AsyncMock()
        await trainer.close()
        trainer._cp.assert_not_awaited()
        trainer.client.disconnect.assert_awaited_once()


class PowerPauseTests(unittest.TestCase):
    def test_requires_positive_recent_measurement(self):
        trainer = ergo.FTMSTrainer()
        self.assertFalse(ergo.has_power(trainer, 10))
        with patch.object(ergo.time, "monotonic", return_value=10):
            trainer._on_data(None, struct.pack("<Hh", 65, 150))
        self.assertTrue(ergo.has_power(trainer, 12))
        self.assertFalse(ergo.has_power(trainer, 14))
        trainer.power = 0
        self.assertFalse(ergo.has_power(trainer, 11))

    def test_workout_waits_and_resumes_without_recording_pauses(self):
        instances = []

        class Trainer(ergo.SimTrainer):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.targets = []
                instances.append(self)

            async def set_power(self, watts):
                self.targets.append((self.calls, watts))

            def tick(self, elapsed):
                self.calls += 1
                super().tick(elapsed)
                self.power = 0 if self.calls in (1, 4) else 120
                if self.calls == 6:
                    self.power_at -= 10  # sem novas notificacoes, valor antigo positivo

        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(workout="unused", ftp=200, sim=True, trainer=None,
                                   address=None, hr=None, overlay=False, film=None,
                                   web=False, mpv_socket=None, yes=True, out=folder)
            with patch.object(ergo, "SimTrainer", Trainer), \
                    patch.object(ergo, "load_workout", return_value=("Teste", [
                        ergo.Block(3, 100, "A"), ergo.Block(3, 150, "B")], False)), \
                    patch("builtins.print"):
                asyncio.run(ergo.run(args))
            trainer = instances[0]
            files = list(Path(folder).glob("*.fit"))
            self.assertEqual(len(files), 1)
            records = ergo.decode_fit(files[0].read_bytes())[20]
            self.assertEqual(len(records), 6)
            self.assertTrue(all(r[7] == 120 for r in records))
            self.assertEqual(trainer.calls, 9)
            self.assertEqual(trainer.targets, [(0, 100), (5, 150)])


if __name__ == "__main__":
    unittest.main()

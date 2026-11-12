"""Bluetooth e simulação."""
import asyncio
import struct
import time

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

    async def release_power(self):
        """Repor o modo livre ao sair de um bloco com alvo ERG."""
        for payload in (b'\x01', b'\x00', b'\x07'):
            await self._cp(payload)

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

    async def release_power(self):
        self.target = 100

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
        dev = await find_device(HRS, address=self.name) if self.name and ':' in self.name else await find_device(HRS, self.name or None)
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



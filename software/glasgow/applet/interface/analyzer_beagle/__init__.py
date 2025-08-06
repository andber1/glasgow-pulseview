import logging
import asyncio
import time
from ..analyzer import AnalyzerApplet


def empty_queue(q: asyncio.Queue):
    while not q.empty():
        q.get_nowait()
        q.task_done()


class BeagleLogicServer:
    """A TCP server that acts as a BeagleLogic analyzer to which a sigrok PulseView can connect."""

    logger = logging.getLogger(__name__)
    host = "127.0.0.1"
    port = 5555

    def __init__(self):
        self.data_queue = asyncio.Queue()
        self.sample_rate = 1_000_000
        self.last_timestamp = None
        self.last_value = 0

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        self.logger.info("Connection from %s", addr)
        stop_event = asyncio.Event()

        try:
            while True:
                data = await reader.read(1024)
                if not data:
                    self.logger.info("Connection closed by %s", addr)
                    break

                data = data.decode("utf-8").strip()
                self.logger.debug("Received message: %s", data)
                response = None
                match data:
                    case "version":
                        response = "BeagleLogic 1.0"
                    case "samplerate":
                        response = str(self.sample_rate)
                    case samplerate if samplerate.startswith("samplerate "):
                        self.sample_rate = int(samplerate.split(" ")[1])
                        self.logger.info("Setting sample rate to %d", self.sample_rate)
                        response = "OK"
                    case "sampleunit":
                        # Per default all 14 channels are activated (two bytes per sample point).
                        # If 1-8 channels are selected the data format would change
                        # (sampleunit=1, i.e. only one byte per sample point instead of two).
                        # Currently, we stick with the default and do not allow to change the data format.
                        response = "0"  # 0:16bits, 1:8bits
                    case "sampleunit 0":
                        response = "OK"
                    case "sampleunit 1":
                        self.logger.error("Changing sampleunit not supported.")
                        response = "ERROR"
                    case "memalloc":
                        response = "67108864"  # 64 MiB
                    case "bufunitsize":
                        response = "4194304"  # 4 MiB
                    case "triggerflags 1":
                        # trigerflags is set by PulseView
                        # Set this to zero '0' for one-shot logic capture (i.e. fill up the buffer once and stop), and '1' for continuously capturing data.
                        response = "OK"
                    case "triggerflags 0":
                        response = "OK"
                    case "get":
                        self.logger.info("Start sending data")
                        empty_queue(self.data_queue)
                        stop_event.clear()

                        async def data_loop(stop_event):
                            while not stop_event.is_set():
                                data = await self.data_queue.get()
                                writer.write(data)
                                await writer.drain()

                        asyncio.create_task(data_loop(stop_event))

                    case "close":
                        self.logger.info("Stop sending data")
                        response = "OK"
                        stop_event.set()

                if response:
                    self.logger.debug("Sending response: %s", response)
                    response = response + "\r\n"
                    writer.write(response.encode("utf-8"))
                    await writer.drain()

        except ConnectionResetError:
            self.logger.info("Connection closed")

        stop_event.set()

    async def send_data(self, timestamp: float, value: int | None):
        if self.last_timestamp is None:
            samples = 1
        else:
            duration = timestamp - self.last_timestamp
            samples = round(duration * self.sample_rate)
            if samples <= 0:
                return

        # BeagleLogic has 14 channels. Data is transmitted in consecutive pairs of bytes (when sampleunit==0).
        # 1. byte: bits 0-7, 2. byte: bits 8-13: currently unused (PulseView will crash if bits 14-15 are set)
        value = (value & 0xFF) if value is not None else self.last_value
        await self.data_queue.put(bytes([self.last_value, 0] * (samples - 1) + [value, 0]))
        self.last_timestamp = timestamp
        self.last_value = value

    async def start_server(self):
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        async with server:
            self.logger.info("Start BeagleLogic server on %s:%s", self.host, self.port)
            await server.serve_forever()


class AnalyzerBeagleApplet(AnalyzerApplet):
    logger = logging.getLogger(__name__)
    help = "stream logic waveforms over TCP"
    description = """
    Capture waveforms, similar to a logic analyzer and stream data
    over TCP so that it can be shown on sigrok PulseView (the Glasgow
    pretends to be a BeagleLogic device).
    """

    @classmethod
    def add_interact_arguments(cls, parser):
        pass  # overwritten, because no vcd file

    async def read_data(self, iface):
        overrun = False
        while not overrun:
            for cycle, events in await iface.read():
                self.timestamp = cycle / self._sample_freq
                self.last_time = time.time()

                if events == "overrun":
                    self.logger.error("FIFO overrun, shutting down")
                    overrun = True
                    break

                if "pin" in events:  # could be also "throttle"
                    value = events["pin"]
                    await self.server.send_data(self.timestamp, value)

    async def background(self):
        PERIOD = 0.01
        while True:
            await asyncio.sleep(PERIOD)
            timestamp = (time.time() - self.last_time - PERIOD) + self.timestamp
            await self.server.send_data(timestamp, None)

    async def interact(self, device, args, iface):
        self.server = BeagleLogicServer()
        self.timestamp = 0
        self.last_time = time.time()

        await asyncio.gather(
            self.server.start_server(),
            self.background(),
            self.read_data(iface),
        )

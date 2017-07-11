from io import BytesIO
from abc import ABCMeta, abstractmethod
from asyncio import Transport, Protocol, Event, Queue

from h2.config import H2Configuration
from h2.events import RequestReceived, DataReceived, StreamEnded, WindowUpdated
from h2.events import ConnectionTerminated, RemoteSettingsChanged
from h2.events import SettingsAcknowledged, ResponseReceived, TrailersReceived
from h2.events import StreamReset, PriorityUpdated
from h2.connection import H2Connection
from h2.exceptions import ProtocolError


def _slice(chunks, size):
    first, first_size, second = [], 0, []
    for chunk in chunks:
        if first_size < size:
            if first_size + len(chunk) <= size:
                first.append(chunk)
            else:
                slice_size = size - first_size
                first.append(chunk[:slice_size])
                second.append(chunk[slice_size:])
        else:
            second.append(chunk)
    return first, second


class Buffer:

    def __init__(self, *, loop):
        self._chunks = []
        self._length = 0
        self._complete_size = -1
        self._complete_event = Event(loop=loop)

    def append(self, data):
        self._chunks.append(data)
        self._length += len(data)

        if self._complete_size != -1:
            if self._length >= self._complete_size:
                self._complete_event.set()

    def eof(self):
        self._complete_event.set()

    async def read(self, size=None):
        if size is None:
            await self._complete_event.wait()
            return b''.join(self._chunks)
        else:
            if size < 0:
                raise ValueError('Size is negative')
            elif size == 0:
                return b''
            else:
                if size > self._length:
                    self._complete_size = size
                    await self._complete_event.wait()
                    self._complete_size = -1
                    self._complete_event.clear()
                data, self._chunks = _slice(self._chunks, size)
                return b''.join(data)


class Connection:
    """
    Holds connection state (write_ready), and manages
    H2Connection <-> Transport communication
    """
    def __init__(self, connection: H2Connection, transport, *, loop):
        self._connection = connection
        self._transport = transport
        self._loop = loop

        self.write_ready = Event(loop=self._loop)
        self.write_ready.set()

    def pause_writing(self):
        self.write_ready.clear()

    def resume_writing(self):
        self.write_ready.set()

    def create_stream(self, stream_id):
        return Stream(self, self._connection, self._transport, stream_id,
                      loop=self._loop)

    def flush(self):
        self._transport.write(self._connection.data_to_send())


class Stream:
    """
    API for working with streams, used by clients and request handlers
    """
    def __init__(self, conn, h2_conn, transport, stream_id,
                 *, loop):
        self._conn = conn
        self._h2_conn = h2_conn
        self._transport = transport

        self.id = stream_id

        self.__buffer__ = Buffer(loop=loop)
        self.__headers__ = Queue(loop=loop)
        self.__window_updated__ = Event(loop=loop)

    async def recv_headers(self):
        return await self.__headers__.get()

    async def recv_data(self, size=None):
        data = await self.__buffer__.read(size)
        self._h2_conn.acknowledge_received_data(len(data), self.id)
        return data

    async def send_headers(self, headers, end_stream=False):
        if not self._conn.write_ready.is_set():
            await self._conn.write_ready.wait()

        self._h2_conn.send_headers(self.id, headers, end_stream=end_stream)
        self._transport.write(self._h2_conn.data_to_send())

    async def send_data(self, data, end_stream=False):
        f = BytesIO(data)
        f_pos, f_last = 0, len(data)

        while True:
            if not self._conn.write_ready.is_set():
                await self._conn.write_ready.wait()

            window = self._h2_conn.local_flow_control_window(self.id)
            if not window:
                self.__window_updated__.clear()
                await self.__window_updated__.wait()
                window = self._h2_conn.local_flow_control_window(self.id)

            f_chunk = f.read(min(window, f_last - f_pos))
            f_pos = f.tell()

            if f_pos == f_last:
                self._h2_conn.send_data(self.id, f_chunk,
                                        end_stream=end_stream)
                self._transport.write(self._h2_conn.data_to_send())
                break
            else:
                self._h2_conn.send_data(self.id, f_chunk)
                self._transport.write(self._h2_conn.data_to_send())


class EventsProcessor:
    """
    H2 events processor, synchronous, not doing any IO, as hyper-h2 itself
    """
    def __init__(self, handler, conn, h2_conn, transport):
        self.handler = handler
        self.conn = conn
        self.h2_conn = h2_conn
        self.transport = transport

        self.processors = {
            RequestReceived: self.process_request_received,
            ResponseReceived: self.process_response_received,
            RemoteSettingsChanged: self.process_remote_settings_changed,
            SettingsAcknowledged: self.process_settings_acknowledged,
            DataReceived: self.process_data_received,
            WindowUpdated: self.process_window_updated,
            TrailersReceived: self.process_trailers_received,
            StreamEnded: self.process_stream_ended,
            StreamReset: self.process_stream_reset,
            PriorityUpdated: self.process_priority_updated,
            ConnectionTerminated: self.process_connection_terminated,
        }

        self.streams = {}  # TODO: streams cleanup

    async def create_stream(self):
        # TODO: check concurrent streams count and maybe wait
        stream_id = self.h2_conn.get_next_available_stream_id()
        stream = self.conn.create_stream(stream_id)
        self.streams[stream_id] = stream
        return stream

    def feed(self, data):
        return self.h2_conn.receive_data(data)

    def close(self):
        self.transport.close()
        self.handler.close()

    def process(self, event):
        try:
            proc = self.processors[event.__class__]
        except KeyError:
            raise NotImplementedError(event)
        else:
            proc(event)

    def process_request_received(self, event: RequestReceived):
        stream = self.conn.create_stream(event.stream_id)
        self.streams[event.stream_id] = stream
        self.handler.accept(stream, event.headers)
        # TODO: check EOF

    def process_response_received(self, event: ResponseReceived):
        self.streams[event.stream_id].__headers__.put_nowait(event.headers)

    def process_remote_settings_changed(self, event: RemoteSettingsChanged):
        pass

    def process_settings_acknowledged(self, event: SettingsAcknowledged):
        pass

    def process_data_received(self, event: DataReceived):
        self.streams[event.stream_id].__buffer__.append(event.data)

    def process_window_updated(self, event: WindowUpdated):
        if event.stream_id > 0:
            self.streams[event.stream_id].__window_updated__.set()

    def process_trailers_received(self, event: TrailersReceived):
        self.streams[event.stream_id].__headers__.put_nowait(event.headers)

    def process_stream_ended(self, event: StreamEnded):
        self.streams[event.stream_id].__buffer__.eof()

    def process_stream_reset(self, event: StreamReset):
        self.handler.cancel(self.streams[event.stream_id])

    def process_priority_updated(self, event: PriorityUpdated):
        pass

    def process_connection_terminated(self, event: ConnectionTerminated):
        self.close()


class H2Protocol(Protocol):
    conn: Connection = None
    processor = None

    def __init__(self, handler, config: H2Configuration, *, loop):
        self.handler = handler
        self.config = config
        self.loop = loop

    def connection_made(self, transport: Transport):
        h2_conn = H2Connection(config=self.config)
        h2_conn.initiate_connection()

        self.conn = Connection(h2_conn, transport, loop=self.loop)
        self.conn.flush()

        self.processor = EventsProcessor(self.handler, self.conn, h2_conn,
                                         transport)

    def data_received(self, data: bytes):
        try:
            events = self.processor.feed(data)
        except ProtocolError:
            self.processor.close()
        else:
            self.conn.flush()
            for event in events:
                self.processor.process(event)
            self.conn.flush()

    def pause_writing(self):
        self.conn.pause_writing()

    def resume_writing(self):
        self.conn.resume_writing()

    def connection_lost(self, exc):
        self.processor.close()


class AbstractHandler(metaclass=ABCMeta):

    @abstractmethod
    def accept(self, stream, headers):
        pass

    @abstractmethod
    def cancel(self, stream):
        pass

    @abstractmethod
    def close(self):
        pass


class NoHandler(AbstractHandler):

    def accept(self, stream, headers):
        raise NotImplementedError('Request handling is not implemented')

    def cancel(self, stream):
        pass

    def close(self):
        pass
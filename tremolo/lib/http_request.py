# Copyright (c) 2023 nggit

import os
import time

from urllib.parse import parse_qs

from tremolo.utils import parse_fields, parse_int
from .http_exceptions import BadRequest, PayloadTooLarge
from .request import Request


class HTTPRequest(Request):
    __slots__ = ('_ip', '_scheme', 'header', 'headers', 'is_valid',
                 'host', 'method', 'url', 'path', 'query_string', 'version',
                 'content_length', 'http_continue', 'http_keepalive',
                 '_upgraded', '_body', '_eof', '_stream', '_read_buf')

    def __init__(self, protocol, header):
        super().__init__(protocol)

        self._ip = None
        self._scheme = None
        self.header = header
        self.headers = header.headers.copy()
        self.is_valid = header.is_valid
        self.host = header.gethost()

        if isinstance(self.host, list):
            self.host = self.host[0]

        self.method = header.getmethod().upper()
        self.url = header.geturl()
        self.path, _, self.query_string = self.url.partition(b'?')
        self.version = header.getversion()

        if self.version != b'1.0':
            self.version = b'1.1'

        self.content_length = -1
        self.http_continue = False
        self.http_keepalive = False
        self._upgraded = False
        self._body = bytearray()
        self._eof = False
        self._stream = None
        self._read_buf = bytearray()

    @property
    def ip(self):
        if not self._ip:
            ip = self.headers.get(b'x-forwarded-for', b'')

            if isinstance(ip, list):
                ip = ip[0]

            ip = ip.strip()

            if ip == b'' and self.client is not None:
                self._ip = self.client[0].encode('utf-8')
            else:
                self._ip = ip[:(ip + b',').find(b',')]

        return self._ip

    @property
    def scheme(self):
        if not self._scheme:
            scheme = self.headers.get(b'x-forwarded-proto', b'').strip()

            if scheme == b'' and self.is_secure:
                self._scheme = b'https'
            else:
                self._scheme = scheme or b'http'

        return self._scheme

    @property
    def content_type(self):
        # don't lower() content-type, as it may contain a boundary
        return self.headers.get(b'content-type', b'application/octet-stream')

    @property
    def transfer_encoding(self):
        return self.headers.getlist(b'transfer-encoding')

    @property
    def upgraded(self):
        return self._upgraded

    @upgraded.setter
    def upgraded(self, value):
        del self._body[:]
        del self._read_buf[:]
        self._upgraded = value

    @property
    def has_body(self):
        return (b'content-length' in self.headers or
                b'transfer-encoding' in self.headers)

    def uid(self, length=32):
        if self.client is None:
            port = self.socket.fileno()
        else:
            port = self.client[1]  # 0 - 65535

        prefix = (
            int.to_bytes((port << 16) | (os.getpid() & 0xffff),
                         4, byteorder='big') +
            int(time.time() * 1e7).to_bytes(8, byteorder='big')
        )  # 12 Bytes

        return prefix + os.urandom(max(4, length - len(prefix)))

    def clear(self):
        self.header.clear()
        self.headers.clear()

        del self._body[:]
        del self._read_buf[:]
        super().clear()

    async def body(self, **kwargs):
        async for data in self.stream(**kwargs):
            self._body.extend(data)

        return self._body

    def read(self, size=-1, timeout=None):
        return self.recv(size, timeout, raw=False)

    def eof(self):
        return self._eof and self._read_buf == b''

    async def recv(self, size=-1, timeout=None, raw=True):
        if size == 0 or self.eof():
            return b''

        if self._stream is None:
            self._stream = self.stream(timeout, raw)

        if size == -1:
            return await self._stream.__anext__()

        if len(self._read_buf) < size:
            async for data in self._stream:
                self._read_buf.extend(data)

                if len(self._read_buf) >= size:
                    break

        data = bytes(self._read_buf[:size])
        del self._read_buf[:size]
        return data

    async def stream(self, timeout=None, raw=False):
        if self._eof:
            return

        if self.http_continue:
            self.protocol.send_continue()

        if not raw and b'chunked' in self.transfer_encoding:
            buf = bytearray()
            agen = super().recv(timeout)
            paused = False
            bytes_unread = 0

            while True:
                if not paused:
                    try:
                        buf.extend(await agen.__anext__())
                    except StopAsyncIteration as exc:
                        if b'\r\n\r\n' not in buf:
                            raise BadRequest(
                                'bad chunked encoding: incomplete read'
                            ) from exc

                if bytes_unread > 0:
                    data = bytes(buf[:bytes_unread])

                    yield data
                    del buf[:bytes_unread]

                    bytes_unread -= len(data)

                    if bytes_unread > 0:
                        continue

                    paused = True
                    del buf[:2]
                else:
                    i = buf.find(b'\r\n')

                    if i == -1:
                        if len(buf) > self.protocol.options['buffer_size'] * 4:
                            raise BadRequest(
                                'bad chunked encoding: no chunk size'
                            )

                        paused = False
                        continue

                    try:
                        chunk_size = parse_int(buf[:i].split(b';', 1)[0], 16)
                    except ValueError as exc:
                        raise BadRequest('bad chunked encoding') from exc

                    if chunk_size <= 0:
                        if b'\r\n\r\n' in buf:
                            break

                        raise BadRequest(
                            'bad chunked encoding: invalid last-chunk'
                        )

                    data = bytes(buf[i + 2:i + 2 + chunk_size])
                    bytes_unread = chunk_size - len(data)

                    yield data

                    if bytes_unread > 0:
                        paused = False
                        del buf[:]
                    else:
                        paused = True
                        del buf[:chunk_size + i + 4]
        else:
            if (self.content_length >
                    self.protocol.options['client_max_body_size']):
                raise PayloadTooLarge

            async for data in super().recv(timeout):
                yield data

        self._eof = True

    @property
    def params(self):
        return self.context

    @property
    def query(self):
        try:
            return self.params['query']
        except KeyError:
            self.params['query'] = {}

            if self.query_string != b'':
                self.params['query'] = parse_qs(
                    self.query_string.decode('latin-1'), max_num_fields=100
                )

            return self.params['query']

    @property
    def cookies(self):
        try:
            return self.params['cookies']
        except KeyError:
            self.params['cookies'] = {}

            if b'cookie' in self.headers:
                cookie = self.headers[b'cookie']

                if isinstance(self.headers[b'cookie'], list):
                    cookie = b';'.join(self.headers[b'cookie'])

                for k, v in parse_fields(cookie):
                    k = k.decode('latin-1')

                    if k in self.params['cookies']:
                        self.params['cookies'][k].append(v.decode('latin-1'))
                    else:
                        self.params['cookies'][k] = [v.decode('latin-1')]

            return self.params['cookies']

    async def form(self, max_size=8 * 1048576, max_fields=100):
        try:
            return self.params['post']
        except KeyError as exc:
            self.params['post'] = {}

            if self.content_type.lower().startswith(
                    b'application/x-www-form-urlencoded'):
                async for data in self.stream():
                    self._body.extend(data)

                    if self.body_size > max_size:
                        raise ValueError('form size limit reached') from exc

                if 2 < self.body_size <= max_size:
                    self.params['post'] = parse_qs(
                        self._body.decode('latin-1'),
                        max_num_fields=max_fields
                    )

            return self.params['post']

    async def files(self, max_files=1024, max_file_size=100 * 1048576):
        if self.eof():
            return

        for key, boundary in parse_fields(self.content_type):
            if key == b'boundary' and boundary:
                break
        else:
            raise BadRequest('missing boundary')

        boundary_size = len(boundary)
        body = bytearray()

        header_size = 0
        body_size = 0
        content_length = 0
        paused = False
        part = None  # represents a file received in a multipart request

        if self._stream is None:
            self._stream = self.stream()

        while max_files > 0:
            data = b''

            if not paused:
                try:
                    data = await self._stream.__anext__()
                except StopAsyncIteration as exc:
                    if self._read_buf.startswith(b'--%s--' % boundary):
                        del self._read_buf[:]  # set eof()
                        return

                    if header_size == 1 or body_size == -1:
                        raise BadRequest(
                            'malformed multipart/form-data: incomplete read'
                        ) from exc

            if part is None:
                self._read_buf.extend(data)
                header_size = self._read_buf.find(b'\r\n\r\n') + 2

                if header_size == 1:
                    if len(self._read_buf) > 8192:
                        raise BadRequest(
                            'malformed multipart/form-data: header too large'
                        )

                    paused = False
                else:
                    body.extend(self._read_buf[header_size + 2:])
                    part = {}

                    # use find() instead of startswith() to ignore the preamble
                    if self._read_buf.find(b'--%s\r\n' % boundary,
                                           0, header_size) != -1:
                        header = self.header.parse(
                            self._read_buf, header_size=header_size
                        ).headers

                        if b'content-disposition' in header:
                            for k, v in parse_fields(
                                    header[b'content-disposition']):
                                part[k.decode('latin-1')] = v.decode('latin-1')

                        if b'content-length' in header:
                            try:
                                content_length = parse_int(
                                    header[b'content-length']
                                )
                            except ValueError as exc:
                                raise BadRequest('bad Content-Length') from exc

                            part['length'] = content_length

                        if b'content-type' in header:
                            part['type'] = header[
                                           b'content-type'].decode('latin-1')
                continue

            body.extend(data)
            body_size = body.find(b'\r\n--%s' % boundary, content_length)

            if body_size == -1:
                if len(body) >= max_file_size > boundary_size + 4:
                    sub_part = part.copy()
                    sub_part['data'] = body[:-boundary_size - 4]
                    sub_part['eof'] = False
                    yield sub_part

                    content_length = max(
                        content_length - (len(body) - boundary_size - 4), 0
                    )
                    del body[:-boundary_size - 4]

                paused = False
                continue

            part['data'] = body[:body_size]
            part['eof'] = True
            yield part

            self._read_buf[:] = body[body_size + 2:]
            content_length = 0
            paused = True
            part = None
            max_files -= 1

            del body[:]

#!/usr/bin/env python3

__all__ = ('app', 'HTTP_HOST', 'HTTP_PORT', 'TEST_FILE')

import asyncio  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

# makes imports relative from the repo directory
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from tremolo import Tremolo  # noqa: E402
from tremolo.exceptions import BadRequest  # noqa: E402

HTTP_HOST = '127.0.0.1'
HTTP_PORT = 28000
TEST_FILE = __file__

app = Tremolo()


@app.on_worker_start
async def worker_start(**worker):
    worker_ctx = worker['context']
    worker_ctx.shared = 0
    worker_ctx.socket_family = 'AF_UNIX'


@app.on_worker_start()
async def worker_start2(**worker):
    pass


@app.on_worker_stop()
async def worker_stop2(**worker):
    pass


@app.on_worker_stop
async def worker_stop(**worker):
    worker_ctx = worker['context']

    if worker_ctx.socket_family == 'AF_UNIX':
        assert worker_ctx.shared == 0
    else:
        assert worker_ctx.shared > 0


@app.on_connect
async def on_connect(**server):
    server['context'].foo = 'bar'

    return True


@app.on_close
async def on_close(**server):
    assert server['context'].foo == 'bar'

    return True


@app.on_request
async def my_request_middleware(**server):
    request = server['request']
    response = server['response']
    worker_ctx = server['worker']
    worker_ctx.shared += 1
    worker_ctx.socket_family = request.socket.family.name
    request.protocol.options['max_queue_size'] = 128

    assert request.ctx.foo == 'bar'

    if not request.is_valid:
        raise BadRequest

    if request.method not in (b'GET', b'POST', b'HEAD'):
        response.set_status(405, 'Method Not Allowed')
        response.set_content_type('text/plain')

        return b'Request method %s is not supported!' % request.method

    # these should appear in the next middlewares or handlers
    response.set_header('X-Foo', 'bar')
    response.set_cookie('sess', 'www')


@app.on_response
async def my_response_middleware(**server):
    response = server['response']

    assert response.headers[b'x-foo'] == [b'X-Foo: bar']
    assert b'Set-Cookie: sess=www; ' in response.headers[b'set-cookie'][0]

    response.set_header(b'X-Foo', b'baz')

    if response.headers[b'_line'][1] == b'503':
        response.set_status(503, b'Under Maintenance')
        response.set_content_type(b'text/plain')

        return b'Under Maintenance'


@app.route('/getheaderline')
async def get_headerline(**server):
    request = server['request']

    assert (b'%s?%s' % (request.path, request.query_string)) == request.url

    # b'GET /getheaderline HTTP/1.1'
    return b'%s %s HTTP/%s' % (
        request.method,
        request.url,
        request.version
    )


@app.route('/getip')
async def get_ip(**server):
    request = server['request']

    # b'127.0.0.1'
    return request.ip


@app.route('/gethost')
async def get_host(**server):
    # b'localhost:28000'
    return server['request'].host


@app.route('/getquery')
async def get_query(**server):
    request = server['request']

    assert request.query['a'] == ['111', 'xyz']
    assert request.query['b'] == ['222']

    data = []

    for name, value in request.query.items():
        data.append('{:s}={:s}'.format(name, value[0]))

    # b'a=111&b=222'
    return '&'.join(data)


@app.route(r'^/page/(?P<page_id>\d+)')
async def get_page(**server):
    # b'101'
    return server['request'].params['path'].get('page_id')


@app.route('/getcookies')
async def get_cookies(**server):
    request = server['request']

    assert request.headers.get(b'cookie') == [b'a=123', b'a=xxx, yyy']
    assert request.cookies['a'] == ['123', 'xxx, yyy']

    # b'a=123, a=xxx, yyy'
    return b', '.join(request.headers.getlist(b'cookie'))


async def coro_acquire(lock):
    await lock.acquire()
    await asyncio.sleep(10)


@app.route('/getlock')
async def get_lock(**server):
    loop = server['loop']
    lock = server['lock']
    tasks = server['context'].tasks

    async with lock:
        yield b'Lock'

    async with lock(5):
        yield b' '

    tasks.append(loop.create_task(coro_acquire(lock)))

    try:
        await asyncio.sleep(0.1)
        await lock.acquire(timeout=0)
    except TimeoutError:
        yield b'was acquired!'
    finally:
        lock.release()


@app.route('/submitform')
async def post_form(**server):
    request = server['request']

    await request.form(limit=8192)

    data = []

    for name, value in request.params['post'].items():
        data.append('{:s}={:s}'.format(name, value[0]))

    # b'user=myuser&pass=mypass'
    return '&'.join(data)


@app.route('/upload')
async def upload(content_type=b'application/octet-stream', **server):
    request = server['request']

    if request.query_string == b'maxqueue':
        request.protocol.options['max_queue_size'] = 0

    try:
        size = int(request.query['size'][0])
        yield (await request.read(0)) + (await request.read(size))
    except KeyError:
        # request.stream()
        async for data in request.read(None):
            yield data

        async for data in request.read(None):
            # should not raised
            raise Exception('EOF!!!')


@app.route('/upload/multipart')
async def upload_multipart(stream=False, **server):
    server['response'].set_content_type(b'text/csv')

    # should be ignored
    yield b''

    yield b'name,length,type,data\r\n'

    # should be ignored
    yield b''

    # stream multipart file upload then send it back as csv
    async for info, data in server['request'].files(1):
        yield b'%s,%d,%s,%s\r\n' % (info['name'].encode(),
                                    info['length'],
                                    info['type'].encode(),
                                    (data[:5] + data[-3:]))

    async for info, data in server['request'].files():
        yield b'%s,%d,%s,%s\r\n' % (info['name'].encode(),
                                    info['length'],
                                    info['type'].encode(),
                                    (data[:5] + data[-3:]))

    async for info, data in server['request'].files():
        # should not raised
        raise Exception('EOF!!!')


@app.route('/download')
async def download(**server):
    await server['response'].sendfile(TEST_FILE, content_type=b'text/plain')


@app.route('/ws')
async def ws_handler(websocket=None, tasks=None, **_):
    if websocket.request.query_string == b'close':
        await websocket.accept()

        # test send close manually
        await websocket.close()

        # this suggests that you want to handle the disconnection manually
        return True

    if websocket.request.query_string == b'ping':
        # test Tasks.create
        task = tasks.create(websocket.accept())

        # await websocket.accept()
        await task

        # WebSocket.recv automatically sends pong
        await websocket.recv()
    else:
        # await websocket.accept()
        # while True: data = await websocket.receive()
        #
        # async iterator implicitly performs WebSocket.accept
        async for data in websocket:
            await websocket.send(data)
            break


@app.route('/timeouts')
async def timeouts(request=None, **_):
    if request.query_string == b'recv':
        # attempt to read body on a GET request
        # should raise a TimeoutError and ended up with a RequestTimeout
        await request.recv(100)
    elif request.query_string == b'handler':
        await asyncio.sleep(10)


@app.route('/reload')
async def reload(request=None, **_):
    yield b'%d' % hash(app)

    if request.query_string != b'':
        mtime = float(request.query_string)

        # simulate a code change
        os.utime(TEST_FILE, (mtime, mtime))

# test multiple ports
app.listen(HTTP_PORT + 1, request_timeout=2, keepalive_timeout=2)
app.listen(HTTP_PORT + 2, app_handler_timeout=1)

# test unix socket
# 'tremolo-test.sock'
app.listen('tremolo-test', debug=False, client_max_body_size=73728)

if __name__ == '__main__':
    app.run(HTTP_HOST, port=HTTP_PORT, debug=True, reload=True,
            client_max_body_size=73728, ws_max_payload_size=73728)

# END

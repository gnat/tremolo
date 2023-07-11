#!/usr/bin/env python3

__all__ = ('app', 'ASGI_PORT',)

import asyncio  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

# makes imports relative from the repo directory
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import tremolo  # noqa: E402

from tests.http_server import HTTP_HOST, HTTP_PORT, TEST_FILE  # noqa: E402

ASGI_PORT = HTTP_PORT + 10


async def app(scope, receive, send):
    assert scope['type'] == 'http'
    more_body = True

    while more_body:
        data = await receive()
        assert data['type'] in ('http.request', 'http.disconnect')

        body = data.get('body', b'')

        if scope['method'] == 'GET':
            assert body == b''

        print(
            '%s: received %d bytes: %s%s' % (
                data['type'], len(body), '.' * min(3, len(body)), body[-10:])
        )

        more_body = data.get('more_body', False)

    headers = [
        (b'content-type', b'text/plain')
    ]

    if scope['path'] == '/download':
        headers.append((b'content-length', b'%d' % os.stat(TEST_FILE).st_size))

        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': headers
        })

        with open(TEST_FILE, 'rb') as f:
            await send({
                'type': 'http.response.body',
                'body': f.read()
            })

        return

    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': headers
    })

    await send({
        'type': 'http.response.body',
        'body': b'Hello world!'
    })

if __name__ == '__main__':
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        print('INFO: uvloop is not installed')

    tremolo.run(app, host=HTTP_HOST, port=ASGI_PORT, debug=True, worker_num=2)
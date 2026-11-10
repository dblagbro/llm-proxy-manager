"""Candidate-only real HTTP/RFB probes. Run on the isolated synthetic network."""
import asyncio
import json
import socket
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

HOST='grok-candidate'
ORIGIN='https://operator.example.test'
TOKEN='synthetic-bridge-test-value'
checks={}
with httpx.Client(base_url=f'http://{HOST}:8443',timeout=10,trust_env=False) as c:
    for port in (5900,6080):
        try:
            with socket.create_connection((HOST,port),2):
                raise AssertionError(f'private VNC port {port} exposed')
        except (ConnectionRefusedError,TimeoutError,OSError):
            checks[f'private_port_{port}_closed']=True
    for route in ('/login','/api/status','/vnc/vnc.html'):
        assert c.get(route).status_code==401
        assert c.get(route,headers={'Cookie':'synthetic=user'}).status_code==403
        assert c.get(route,headers={'Cookie':'synthetic=admin'}).status_code==200
    checks['anonymous_denied_nonadmin_denied_admin_http_allowed']=True
    assert c.get('/api/status',headers={'X-Bridge-Token':TOKEN}).status_code==200
    assert c.post('/api/chat',headers={'X-Bridge-Token':'wrong'}).status_code==401
    assert c.post('/api/conversation/new',headers={'Cookie':'synthetic=admin','Origin':'https://wrong.example'}).status_code==403
    assert c.post('/api/login/start',headers={'Cookie':'synthetic=admin'}).status_code==403
    checks['token_and_origin_enforced']=True
    assert c.get('/healthz').status_code==200
    assert c.get('/healthz').json()['version']=='1.2.0'
    checks['browser_health_version']=True


async def probe():
    uri=f'ws://{HOST}:8443/vnc/websockify'
    for headers,origin in [({},ORIGIN),({'Cookie':'synthetic=user'},ORIGIN),({'Cookie':'synthetic=admin'},'https://wrong.example')]:
        try:
            async with connect(uri,additional_headers=headers,origin=origin,proxy=None,open_timeout=3):
                raise AssertionError('unauthorized WebSocket opened')
        except InvalidStatus as exc:
            assert exc.response.status_code==403
    checks['real_websocket_denials']=True
    async with connect(uri,additional_headers={'Cookie':'synthetic=admin'},origin=ORIGIN,
                       subprotocols=['binary'],proxy=None,open_timeout=3) as ws:
        buffer=bytearray()
        async def read(count):
            while len(buffer)<count:
                value=await asyncio.wait_for(ws.recv(),5)
                assert isinstance(value,bytes)
                buffer.extend(value)
            result=bytes(buffer[:count]);del buffer[:count];return result
        assert await read(12)==b'RFB 003.008\n'
        await ws.send(b'RFB 003.008\n')
        count=(await read(1))[0]
        security=await read(count)
        assert 1 in security
        await ws.send(b'\x01')
        assert await read(4)==b'\x00\x00\x00\x00'
        await ws.send(b'\x01')
        initialization=await read(24)
        width=int.from_bytes(initialization[:2],'big');height=int.from_bytes(initialization[2:4],'big')
        assert (width,height)==(1920,1080)
        checks['authenticated_real_rfb_handshake']={'width':width,'height':height}


asyncio.run(probe())
print(json.dumps({'checks':checks,'real_account_used':False,'production_modified':False}))

from fastapi.testclient import TestClient
from test_server import server
from web_service.notification_queue import NotificationQueue
from web_service.server import create_test_server


def test_injected_webhook_is_mounted_and_timer_does_not_poll(server):
    login=server.client()
    assert login.get('/api/me').status_code==200
    class Direct:
        configured=True
        def collect(self,*args,**kwargs): raise AssertionError('timer must not poll')
    class Runtime:
        def __init__(self): self.inbox=NotificationQueue(server.db.connect,server.db.clock)
        def tick(self): return 'empty'
    runtime=Runtime()
    app=create_test_server(server.settings,server.db,server.identity,server.graph,Direct(),server.sender,
                           worker=False,notification_runtime=runtime)
    before=server.graph.calls
    app.state.integration.tick()
    assert server.graph.calls==before
    with TestClient(app,base_url=server.settings.origin) as client:
        reply=client.post('/hooks/graph',params={'validationToken':'test token'})
        assert reply.status_code==200 and reply.text=='test token'

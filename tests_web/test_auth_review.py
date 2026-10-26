import pytest
from test_server import server, subscription


def device_for(server,client):
    return server.db.session_device(server.db.session(client.cookies.get('__Host-calendarsync_session')))


@pytest.mark.parametrize('expiry',['idle','absolute','cleaned'])
def test_expired_view_can_explicitly_revoke_its_own_device(server,expiry):
    client=server.client(); device=device_for(server,client)
    client.post('/api/push',json=subscription())
    other=server.client(); other_device=device_for(server,other)
    if expiry=='idle': server.now[0]+=server.settings.idle_seconds+1
    elif expiry=='absolute': server.now[0]+=server.settings.session_seconds+1
    else: server.db.revoke_session(client.cookies.get('__Host-calendarsync_session'))
    result=client.post('/auth/logout',headers={'X-CalendarSync-Logout':'1'})
    assert result.status_code==204
    assert server.db.device(device['id_hash']) is None
    assert server.db.subscriptions(device['id_hash'])==[]
    assert server.db.device(other_device['id_hash']) is not None
    with server.db.connect() as db:
        row=db.execute('SELECT credential_blob FROM web_connections WHERE id_hash=?',(device['id_hash'],)).fetchone()
        assert server.db.unseal(row[0])=={}
    assert client.post('/auth/logout',headers={'X-CalendarSync-Logout':'1'}).status_code==204


def test_device_logout_requires_csrf_protection_and_rejects_cross_origin(server):
    client=server.client(); device=device_for(server,client)
    client.headers.pop('x-csrf-token')
    assert client.post('/auth/logout').status_code==403
    assert client.post('/auth/logout',headers={'X-CalendarSync-Logout':'1','Origin':'https://evil.example'}).status_code==403
    assert client.post('/auth/logout',headers={'X-CalendarSync-Logout':'1','Sec-Fetch-Site':'same-site'}).status_code==403
    assert server.db.device(device['id_hash']) is not None
    anonymous=server.client(None)
    assert anonymous.post('/auth/logout',headers={'X-CalendarSync-Logout':'1'}).status_code==204
    assert server.db.device(device['id_hash']) is not None


def assert_host_deletions(response):
    cookies=response.headers.get_list('set-cookie')
    deleted=[cookie for cookie in cookies if cookie.startswith('__Host-') and 'Max-Age=0' in cookie]
    assert deleted
    for cookie in deleted:
        attributes={part.strip().lower() for part in cookie.split(';')[1:]}
        assert {'secure','httponly','path=/','samesite=lax'}<=attributes
        assert not any(value.startswith('domain=') for value in attributes)


@pytest.mark.parametrize('route',['/auth/logout','/auth/logout-all'])
def test_https_logout_deletes_host_cookies_with_valid_prefix_attributes(server,route):
    client=server.client()
    response=client.post(route)
    assert response.status_code==204
    assert_host_deletions(response)
    assert len([cookie for cookie in response.headers.get_list('set-cookie') if 'Max-Age=0' in cookie])==2


@pytest.mark.parametrize('code',['ok','invalid'])
def test_callback_deletes_host_flow_cookie_with_valid_prefix_attributes(server,code):
    client=server.client(None)
    client.get('/auth/login',follow_redirects=False)
    response=client.get('/auth/callback?state=s&code='+code,follow_redirects=False)
    assert_host_deletions(response)

"""A failed account-wide listing must never publish an empty permission proof."""
import logging

import httpx
import pytest

from web_service.school_graph import BASE, DelegatedGraph, GraphFailure


USER = '33333333-3333-4333-8333-333333333333'
TEAM = '22222222-2222-4222-8222-222222222222'
TENANT = '11111111-1111-4111-8111-111111111111'


def graph_with(handler):
    def route(request):
        if request.url.path == '/v1.0/me':
            return httpx.Response(200, json={'id': USER, 'userType': 'Member'})
        return handler(request)
    return DelegatedGraph(lambda: httpx.Client(transport=httpx.MockTransport(route)))


@pytest.mark.parametrize('status', [403, 404])
@pytest.mark.parametrize('later_page', [False, True])
def test_unavailable_joined_teams_is_not_empty_success(status, later_page, caplog):
    caplog.set_level(logging.INFO, logger='uvicorn.error')
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == '/v1.0/me/joinedTeams'
        if later_page and len(calls) == 1:
            return httpx.Response(200, json={
                'value': [{'id': TEAM, 'displayName': 'PRIVATE_TEAM_NAME'}],
                '@odata.nextLink': BASE + '/me/joinedTeams?$skiptoken=PRIVATE_CURSOR'})
        return httpx.Response(status, json={'error': {'message': 'PRIVATE_ERROR_BODY'}})
    with pytest.raises(GraphFailure) as error:
        graph_with(handler).channels('PRIVATE_BEARER_TOKEN', USER, TENANT)
    assert error.value.state == 'verification_failed'
    assert len(calls) == (2 if later_page else 1)
    assert 'listing_unavailable' in caplog.text
    for private in ('PRIVATE_TEAM_NAME', 'PRIVATE_CURSOR', 'PRIVATE_ERROR_BODY', 'PRIVATE_BEARER_TOKEN'):
        assert private not in caplog.text


def test_successful_empty_joined_teams_remains_valid():
    def handler(request):
        assert request.url.path == '/v1.0/me/joinedTeams'
        return httpx.Response(200, json={'value': []})
    assert graph_with(handler).channels('token', USER, TENANT) == []


@pytest.mark.parametrize('status', [403, 404])
def test_denied_individual_team_does_not_grant_its_channels(status):
    def handler(request):
        if request.url.path == '/v1.0/me/joinedTeams':
            return httpx.Response(200, json={'value': [{'id': TEAM}]})
        assert request.url.path == f'/v1.0/teams/{TEAM}/allChannels'
        return httpx.Response(status, json={'error': 'denied'})
    assert graph_with(handler).channels('token', USER, TENANT) == []


def test_denied_channel_probe_still_removes_access():
    def handler(request):
        if request.url.path == '/v1.0/me/joinedTeams':
            return httpx.Response(200, json={'value': [{'id': TEAM}]})
        if request.url.path.endswith('/allChannels'):
            return httpx.Response(200, json={'value': [{'id': 'channel', 'displayName': 'Class'}]})
        assert request.url.path.endswith('/channels/channel/messages')
        return httpx.Response(403, json={'error': 'denied'})
    assert graph_with(handler).channels('token', USER, TENANT) == []

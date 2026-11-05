import pytest

from web_service.notification_target import parse_notification_target


TEAM = 'team'
CHANNEL = '19:abc@thread.tacv2'


@pytest.mark.parametrize('path,expected', [
    ('/teams/team/channels/19%3Aabc%40thread.tacv2/messages/123',
     {'message_id': '123', 'reply_id': None}),
    ("teams('team')/channels('19:abc@thread.tacv2')/messages('123')",
     {'message_id': '123', 'reply_id': None}),
    ("/teams('team')/channels('19%3Aabc%40thread.tacv2')/messages('123')/replies('456')",
     {'message_id': '123', 'reply_id': '456'}),
    ('teams/team/channels/19:abc@thread.tacv2/messages/123/replies/456',
     {'message_id': '123', 'reply_id': '456'}),
])
def test_only_identifiers_from_recognized_same_channel_path_are_returned(path, expected):
    assert parse_notification_target(path, TEAM, CHANNEL) == expected


@pytest.mark.parametrize('resource', [
    'https://graph.microsoft.com/v1.0/teams/team/channels/19:abc@thread.tacv2/messages/123',
    '//teams/team/channels/19:abc@thread.tacv2/messages/123',
    '/teams/other/channels/19:abc@thread.tacv2/messages/123',
    '/teams/team/channels/other/messages/123',
    "/teams('other')/channels('19:abc@thread.tacv2')/messages('123')",
    '/teams/team/channels/19:abc@thread.tacv2/messages/123?redirect=evil',
    '/teams/team/channels/19:abc@thread.tacv2/messages/123#fragment',
    '/teams/team/channels/19:abc@thread.tacv2/messages/123/replies',
    '/teams/team/channels/19:abc@thread.tacv2/messages/123/replies/456/extra',
    '/teams/team/channels/19:abc@thread.tacv2/messages/%2e%2e',
    '/teams/team/channels/19:abc@thread.tacv2/messages/abc%2Fdef',
    '/teams/team/channels/19:abc@thread.tacv2/messages/abc%252Fdef',
    '/teams/team/channels/19:abc@thread.tacv2/messages/abc%5Cdef',
    '/teams/team/channels/19:abc@thread.tacv2/messages/%0A',
    '/teams/team/channels/19:abc@thread.tacv2/messages/%FF',
    '/teams/team/channels/19:abc@thread.tacv2/messages/123\n',
    '/teams/team/channels/19:abc@thread.tacv2/messages/123 ',
    None,
    {'resource': 'unexpected'},
])
def test_untrusted_or_unrecognized_path_is_not_forwardable(resource):
    assert parse_notification_target(resource, TEAM, CHANNEL) is None


def test_payload_cannot_supply_subscription_identity():
    path = '/teams/team/channels/19:abc@thread.tacv2/messages/123'
    assert parse_notification_target(path, 'other', CHANNEL) is None
    assert parse_notification_target(path, TEAM, 'other') is None

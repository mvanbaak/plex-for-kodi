# coding=utf-8
"""lib/plexpeople.py - plex.tv/community people lookups for Watch Together."""

from __future__ import absolute_import

import json

from lib import plexpeople


def test_friends_parses_graphql():
    body = json.dumps({"data": {"allFriendsV2": [
        {"user": {"id": "abc", "idRaw": 1000002, "displayName": "P", "avatar": "http://a"}},
    ]}}).encode()
    out = plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", body))
    assert out == [{"id": 1000002, "title": "P", "thumb": "http://a"}]


def test_friends_degrades_on_error():
    assert plexpeople.friends("tok", http=lambda *a, **k: (500, "text/html", b"")) == []


def test_friends_empty_is_empty():
    body = json.dumps({"data": {"allFriendsV2": []}}).encode()
    assert plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", body)) == []


def test_friends_posts_graphql_with_token_header():
    calls = []

    def http(method, url, headers, body=None):
        calls.append((method, url, headers, body))
        return (200, "application/json", b'{"data": {"allFriendsV2": []}}')

    plexpeople.friends("tok", http=http)
    method, url, headers, body = calls[0]
    assert method == "POST"
    assert url == "https://community.plex.tv/api"
    assert headers["x-plex-token"] == "tok"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body.decode("utf-8"))["operationName"] == "GetAllFriends"


def test_friends_skips_entries_without_idraw():
    body = json.dumps({"data": {"allFriendsV2": [
        {"user": {"displayName": "no id"}},
        {"user": {"idRaw": 7, "displayName": "ok"}},
    ]}}).encode()
    out = plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", body))
    assert out == [{"id": 7, "title": "ok", "thumb": ""}]


def test_friends_degrades_on_bad_json():
    assert plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", b"<html>")) == []


def test_friends_non_dict_json_is_empty():
    assert plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", b"[]")) == []


def test_friends_scalar_json_is_empty():
    assert plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", b"123")) == []


def test_friends_non_dict_data_is_empty():
    body = json.dumps({"data": [1]}).encode()
    assert plexpeople.friends("tok", http=lambda *a, **k: (200, "application/json", body)) == []


def test_shared_users_parses_xml():
    xml = b'<MediaContainer><SharedServer userID="1000002" username="p"/></MediaContainer>'
    out = plexpeople.shared_users("tok", "mid", http=lambda *a, **k: (200, "application/xml", xml))
    assert out == [{"id": 1000002, "title": "p"}]


def test_shared_users_404_is_empty():
    assert plexpeople.shared_users("tok", "mid", http=lambda *a, **k: (404, "application/xml", b"")) == []


def test_shared_users_gets_server_endpoint_with_token():
    calls = []

    def http(method, url, headers, body=None):
        calls.append((method, url, headers, body))
        return (200, "application/xml", b"<MediaContainer/>")

    plexpeople.shared_users("tok", "mid", http=http)
    method, url, headers, body = calls[0]
    assert method == "GET"
    assert url == "https://plex.tv/api/servers/mid/shared_servers"
    assert headers["x-plex-token"] == "tok"


def test_shared_users_degrades_on_bad_xml():
    assert plexpeople.shared_users("tok", "mid",
                                   http=lambda *a, **k: (200, "application/xml", b"<not xml")) == []

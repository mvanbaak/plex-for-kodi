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

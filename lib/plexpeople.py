# coding=utf-8
"""plex.tv / community people lookups for Watch Together.

Deliberately Kodi-free: the unit tests import this module directly, so it must
not pull in ``xbmc`` or ``lib.kodi_util``. The Kodi layer only wires it in.
"""

from __future__ import absolute_import

import json
import xml.etree.ElementTree as ET

try:
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
except ImportError:  # Python 2
    from urllib2 import Request, urlopen, HTTPError


GRAPHQL_URL = "https://community.plex.tv/api"
SHARED_SERVERS_URL = "https://plex.tv/api/servers/{machine_id}/shared_servers"

# PM4K identity headers. CLIENT_ID is a module attribute so the Kodi caller can
# set it from plex.CLIENT_ID without this module importing Kodi.
CLIENT_ID = ""
PRODUCT = "PM4K"
PLATFORM = "Kodi"
VERSION = "1.0"

_FRIENDS_QUERY = ("query GetAllFriends { allFriendsV2 { user { avatar displayName "
                  "id idRaw username } createdAt } }")


def _http(method, url, headers, body=None):
    """Minimal stdlib transport. Never raises on HTTP errors: failures come
    back as a ``0`` status so callers can degrade."""
    req = Request(url, data=body, headers=headers)
    if method:
        req.get_method = lambda: method
    try:
        resp = urlopen(req)
    except HTTPError as exc:
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        return exc.code, content_type, exc.read()
    except Exception:
        return 0, "", b""
    try:
        return resp.getcode(), resp.headers.get("Content-Type", ""), resp.read()
    finally:
        resp.close()


def _headers(token):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-plex-token": token,
        "x-plex-client-identifier": CLIENT_ID,
        "x-plex-product": PRODUCT,
        "x-plex-platform": PLATFORM,
        "x-plex-version": VERSION,
    }


def friends(token, http=None):
    """Return ``[{"id": int, "title": str, "thumb": str}]`` for the user's Plex
    friends, or ``[]`` on any failure (the picker then falls back to home
    users). Never logs the token or the response body."""
    http = http or _http
    body = json.dumps({"query": _FRIENDS_QUERY,
                       "operationName": "GetAllFriends"}).encode("utf-8")
    status, _, content = http("POST", GRAPHQL_URL, _headers(token), body)
    if not 200 <= status < 300:
        return []
    try:
        data = json.loads(content.decode("utf-8"))
        payload = data.get("data") if isinstance(data, dict) else None
        edges = payload.get("allFriendsV2") if isinstance(payload, dict) else None
        if not isinstance(edges, list):
            return []
    except (ValueError, AttributeError, TypeError):
        return []

    out = []
    for edge in edges:
        user = edge.get("user") if isinstance(edge, dict) else None
        user_id = user.get("idRaw") if isinstance(user, dict) else None
        if user_id is None:
            continue
        out.append({"id": user_id,
                    "title": user.get("displayName", ""),
                    "thumb": user.get("avatar", "")})
    return out


def shared_users(token, machine_id, http=None):
    """Return ``[{"id": int, "title": str}]`` for the users a server is shared
    with, or ``[]`` on any failure. Owner-only: a server shared *to* the token
    holder answers 404, which degrades to ``[]``. Never logs the token or the
    response body."""
    http = http or _http
    headers = _headers(token)
    headers["Accept"] = "application/xml"
    status, _, content = http("GET", SHARED_SERVERS_URL.format(machine_id=machine_id),
                              headers)
    if not 200 <= status < 300:
        return []
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return []

    out = []
    for node in root.iter():
        user_id = node.get("userID")
        if user_id is None:
            continue
        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            continue
        out.append({"id": user_id, "title": node.get("username", "")})
    return out

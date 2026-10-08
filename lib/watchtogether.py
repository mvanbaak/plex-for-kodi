# coding=utf-8
"""Watch Together rooms REST model — https://together.plex.tv (§4).

No Kodi: the plex.tv account token is injected by the caller. Transport is
injectable for tests. Rules baked in from §4: DELETE means "leave" (never
"destroy"); a room you left reads 403 while an expired one reads 404;
never read or log a 401 body — it leaks internal service URLs.
"""

from __future__ import absolute_import

import requests

BASE = "https://together.plex.tv"


class WatchTogetherError(Exception):
    pass


class AuthError(WatchTogetherError):
    """401 — message carries method+path only, never the response body."""


class NotMember(WatchTogetherError):
    """403 on GET — you are not (or no longer) in this room."""


class RoomGone(WatchTogetherError):
    """404 — expired (endsAt passed) or never existed. Never retry."""


class Room(object):
    def __init__(self, data):
        self.raw = data
        self.id = data.get("id")
        self.title = data.get("title")
        self.source_uri = data.get("sourceUri") or data.get("source")
        self.created_by = data.get("createdBy")
        self.starts_at = data.get("startsAt", 0)
        # null endsAt = no scheduled end; inf keeps ended() total (0/None
        # would make every comparison True or TypeError)
        self.ends_at = data.get("endsAt") or float("inf")
        self.syncplay_host = data.get("syncplayHost")
        self.syncplay_port = data.get("syncplayPort")
        self.users = data.get("users") or []

    @property
    def user_ids(self):
        # users[] can contain the inviter twice — treat as a set (§4)
        return set(u.get("id") for u in self.users if isinstance(u, dict))

    @property
    def participants(self):
        seen = {}
        for user in self.users:
            if isinstance(user, dict) and user.get("id") is not None:
                seen.setdefault(user["id"], user)
        return list(seen.values())

    def ended(self, now=None):
        import time as _time
        return (now if now is not None else _time.time()) >= self.ends_at


def _http_request(method, path, body=None, token=None):
    headers = {"Accept": "application/json", "X-Plex-Token": token or ""}
    # allow_redirects=False: never follow a 3xx with the account token —
    # requests only strips Authorization, custom headers survive the hop
    kwargs = {"headers": headers, "timeout": 15, "allow_redirects": False}
    if body is not None:
        kwargs["json"] = body
    resp = requests.request(method, BASE + path, **kwargs)
    try:
        payload = resp.json() if resp.content else None
    except ValueError:
        payload = None
    return resp.status_code, payload


class RoomsApi(object):
    """The §4 surface used by v1: list, fetch, leave. create/invite = v2."""

    def __init__(self, token, transport=None):
        self.token = token
        self._transport = transport or _http_request

    def _req(self, method, path, body=None):
        try:
            status, payload = self._transport(method, path, body, self.token)
        except requests.RequestException as exc:
            # class name only — str(exc) can embed the URL/token query
            raise WatchTogetherError("%s %s -> %s" % (method, path,
                                                      exc.__class__.__name__))
        if status == 401:
            # §4: body is untrusted and the first 401 leaks an internal URL
            raise AuthError("%s %s -> 401" % (method, path))
        if status == 403:
            raise NotMember("%s %s -> 403" % (method, path))
        if status == 404:
            raise RoomGone("%s %s -> 404" % (method, path))
        if 300 <= status < 400:
            raise WatchTogetherError("%s %s -> HTTP %d redirect" % (method, path,
                                                                    status))
        if status >= 400:
            raise WatchTogetherError("%s %s -> HTTP %d" % (method, path, status))
        return payload

    def rooms(self):
        payload = self._req("GET", "/rooms") or {"rooms": []}
        return [Room(r) for r in payload.get("rooms") or []]

    def room(self, room_id):
        payload = self._req("GET", "/rooms/%s" % room_id)
        if not payload:
            raise WatchTogetherError("GET /rooms/%s -> empty body" % room_id)
        return Room(payload)

    def leave(self, room_id):
        """DELETE is per-participant leave (§4); 404 means already gone."""
        try:
            self._req("DELETE", "/rooms/%s" % room_id)
        except RoomGone:
            pass

    def create(self, source_uri, title, users=None):
        raise NotImplementedError("v2: host capability from Kodi")

    def invite(self, room_id, user_ids):
        raise NotImplementedError("v2: host capability from Kodi")

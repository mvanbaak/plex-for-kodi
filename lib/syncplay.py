# coding=utf-8
"""Plex Watch Together syncplay session — protocol logic.

No Kodi imports and no sockets: transport is injected. Feed parsed JSON
messages to Session.on_message(); pull outbound messages from
Session.outbound_state() on a steady 1 Hz cadence. Protocol reference:
docs/watch-together.md §5–§6.
"""

from __future__ import absolute_import

import json
import time

VERSION = "1.6.4"
MAX_IDENTITY_BYTES = 149   # relay blind-truncates at 150 (§5.1); stay under
SEEK_BEHIND = -1.75        # §6.2 thresholds
SEEK_AHEAD = 4.0
TEMPO_DIFF = 1.5
TEMPO_RATE = 0.95


def build_identity(device_identifier, device_name, user_id):
    """The double-encoded identity string (§5.1). Compact, under 150 bytes."""
    ident = json.dumps(
        {"deviceIdentifier": device_identifier,
         "deviceName": device_name,
         "userID": str(user_id)},
        separators=(",", ":"))
    if len(ident.encode("utf-8")) > MAX_IDENTITY_BYTES:
        raise ValueError(
            "identity is %d bytes; the relay truncates at 150 and the result "
            "is unparseable — shorten deviceName" % len(ident.encode("utf-8")))
    return ident


def strip_identity(raw):
    """Strip the relay's collision-ladder underscores before parsing (§5.1)."""
    if isinstance(raw, str):
        return raw.rstrip("_")
    return raw


def is_self(set_by, identity):
    """True when setBy resolves to the identity WE sent (§5.5).

    The relay echoes our own State back with setBy filled in; applying it
    makes us fight our own playback. Matched as a subset on str() so it
    survives key reordering, extra fields and int-vs-str ids — full dict
    equality silently stops self-ignoring on any of those.
    """
    if not set_by or not identity:
        return False
    try:
        theirs = json.loads(strip_identity(set_by))
        mine = json.loads(strip_identity(identity))
    except (ValueError, TypeError):
        return False
    if not isinstance(theirs, dict) or not isinstance(mine, dict):
        return False
    return all(k in theirs and str(theirs[k]) == str(v) for k, v in mine.items())


def hello(room, identity, version=VERSION):
    """Hello, sent immediately on connect (§5.2)."""
    return {"Hello": {"room": {"name": room},
                      "username": identity,
                      "version": version}}


def list_request():
    """Roster snapshot request (§5.3)."""
    return {"List": {}}


def set_ready(is_ready, manually_initiated=True):
    """Readiness for the lobby/ready flow — sent only on change (§6.4)."""
    return {"Set": {"ready": {"isReady": bool(is_ready),
                              "manuallyInitiated": bool(manually_initiated)}}}


def set_file(uri, playing=False):
    """Announce what is playing (§5.4). name is double-encoded JSON."""
    inner = json.dumps({"ads": {"playing": bool(playing)}, "uri": uri},
                       separators=(",", ":"))
    return {"Set": {"file": {"name": inner}}}

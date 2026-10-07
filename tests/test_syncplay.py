# coding=utf-8
"""Tests for lib/syncplay.py — identity, builders, drift math, session.

Protocol references are docs/watch-together.md sections (§5.1 etc.)."""

from __future__ import absolute_import

import json
import unittest

from lib import syncplay


class IdentityTest(unittest.TestCase):
    def test_builds_compact_double_encodable_string(self):
        ident = syncplay.build_identity("dev-1234", "Kodi", 1000002)
        parsed = json.loads(ident)                 # the string itself is JSON
        self.assertEqual(parsed["deviceIdentifier"], "dev-1234")
        self.assertEqual(parsed["deviceName"], "Kodi")
        self.assertEqual(parsed["userID"], "1000002")

    def test_compact_separators(self):
        ident = syncplay.build_identity("d", "K", 1)
        self.assertNotIn(" ", ident, "relay echoes bytes as-is; stay compact (§5.1)")

    def test_numeric_user_id_becomes_string(self):
        ident = syncplay.build_identity("d", "K", 2028816)
        self.assertIn('"userID":"2028816"', ident)

    def test_rejects_identity_at_150_bytes(self):
        # 150+ bytes get blind-truncated mid-string by the relay (§5.1)
        with self.assertRaises(ValueError):
            syncplay.build_identity("device-identifier-1234567890",
                                    "way-too-long-device-name-for-the-150-byte"
                                    "-identity-cap-limit-pad",
                                    1000001)

    def test_short_device_name_fits(self):
        ident = syncplay.build_identity("device-identifier-1234567890", "Kodi", 1000001)
        self.assertLessEqual(len(ident.encode()), 149)

    def test_strip_identity_removes_collision_ladder_underscores(self):
        self.assertEqual(syncplay.strip_identity("abc_"), "abc")
        self.assertEqual(syncplay.strip_identity("abc___"), "abc")
        self.assertEqual(syncplay.strip_identity("abc"), "abc")
        self.assertEqual(syncplay.strip_identity(None), None)

    def test_is_self_matches_own_identity(self):
        ident = syncplay.build_identity("d", "Kodi", 1000001)
        self.assertTrue(syncplay.is_self(ident, ident))

    def test_is_self_survives_relay_appended_underscores(self):
        ident = syncplay.build_identity("d", "Kodi", 1000001)
        self.assertTrue(syncplay.is_self(ident + "__", ident))

    def test_is_self_survives_reordered_or_extended_setby(self):
        # twoclientprobe.py proved subset matching is what keeps self-ignore
        # alive across key reorder / extra fields / int-vs-str ids (§5.5)
        mine = json.dumps({"deviceIdentifier": "d", "deviceName": "Kodi",
                           "userID": "1000001"}, separators=(",", ":"))
        theirs = json.dumps({"userID": 1000001, "deviceIdentifier": "d",
                             "deviceName": "Kodi", "extra": "field"},
                            separators=(",", ":"))
        self.assertTrue(syncplay.is_self(theirs, mine))

    def test_is_self_rejects_other_identity(self):
        mine = syncplay.build_identity("d", "Kodi", 1000001)
        theirs = syncplay.build_identity("d", "Safari", 1000002)
        self.assertFalse(syncplay.is_self(theirs, mine))

    def test_is_self_rejects_none_and_garbage(self):
        ident = syncplay.build_identity("d", "K", 1)
        self.assertFalse(syncplay.is_self(None, ident))
        self.assertFalse(syncplay.is_self("not json", ident))
        self.assertFalse(syncplay.is_self(json.dumps(["list"]), ident))


class BuildersTest(unittest.TestCase):
    def test_hello_matches_wire_shape(self):
        ident = syncplay.build_identity("d", "K", 1)
        self.assertEqual(
            syncplay.hello("ca8cfezmke4", ident),
            {"Hello": {"room": {"name": "ca8cfezmke4"},
                       "username": ident,
                       "version": "1.6.4"}})

    def test_list_request(self):
        self.assertEqual(syncplay.list_request(), {"List": {}})

    def test_set_ready(self):
        self.assertEqual(
            syncplay.set_ready(True, manually_initiated=True),
            {"Set": {"ready": {"isReady": True, "manuallyInitiated": True}}})

    def test_set_file_double_encodes_uri(self):
        msg = syncplay.set_file("server://aaaa/com.plexapp.plugins.library/"
                                "library/metadata/227117")
        inner = json.loads(msg["Set"]["file"]["name"])   # name is a JSON string
        self.assertEqual(inner["uri"],
                         "server://aaaa/com.plexapp.plugins.library/"
                         "library/metadata/227117")
        self.assertEqual(inner["ads"], {"playing": False})

    def test_set_file_compact_inner(self):
        msg = syncplay.set_file("x")
        self.assertNotIn(" ", msg["Set"]["file"]["name"])

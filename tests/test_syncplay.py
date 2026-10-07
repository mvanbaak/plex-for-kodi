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

class LatencyTest(unittest.TestCase):
    def bootstrap(self, sr, lc, now):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": sr, "clientLatencyCalculation": lc}, now)
        return lat

    def test_first_sample_seeds_from_server_rtt(self):
        # §6.1: if averageRtt == 0: averageRtt = ping.serverRtt
        lat = self.bootstrap(sr=0.057, lc=100.0, now=100.1)
        expected_avg = 0.85 * 0.057 + 0.15 * 0.1
        self.assertAlmostEqual(lat.avg_rtt, expected_avg, places=6)
        expected = expected_avg / 2 + (0.1 - 0.057)   # sr < clientRtt -> skew add
        self.assertAlmostEqual(lat.forward_delay, expected, places=6)

    def test_no_skew_add_when_server_rtt_is_larger(self):
        lat = self.bootstrap(sr=3600.06, lc=100.0, now=100.1)
        self.assertAlmostEqual(lat.forward_delay, lat.avg_rtt / 2, places=6)

    def test_negative_client_rtt_sample_is_skipped(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": 0.05, "clientLatencyCalculation": 200.0}, 100.0)
        self.assertEqual(lat.client_rtt, 0.0, "clock ahead of sample: skip")
        self.assertEqual(lat.avg_rtt, 0.0)

    def test_negative_server_rtt_sample_is_skipped(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": -3599.94, "clientLatencyCalculation": 100.0},
                     100.1)
        self.assertEqual(lat.client_rtt, 0.0)

    def test_server_rtt_stored_for_outbound_echo_even_when_sample_skipped(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": -3599.94, "clientLatencyCalculation": 100.0},
                     100.1)
        self.assertEqual(lat.server_rtt, -3599.94)

    def test_missing_client_latency_calculation_updates_nothing(self):
        lat = syncplay.Latency()
        lat.on_state({"serverRtt": 0.2}, 100.0)
        self.assertEqual(lat.client_rtt, 0.0)
        self.assertEqual(lat.server_rtt, 0.2, "serverRtt is echoed back out (§5.5)")

    def test_ema_walk(self):
        lat = syncplay.Latency()
        lat.avg_rtt = 1.0
        lat.on_state({"serverRtt": 1.0, "clientLatencyCalculation": 0.0}, 2.0)
        self.assertAlmostEqual(lat.avg_rtt, 0.85 * 1.0 + 0.15 * 2.0, places=6)


class SyncActionTest(unittest.TestCase):
    def test_seeks_when_ahead_by_4_or_more(self):
        action = syncplay.sync_action(local_position=104.0,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("seek", 100.0))

    def test_seeks_when_behind_by_more_than_1_75(self):
        action = syncplay.sync_action(local_position=98.0,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("seek", 100.0))

    def test_boundary_1_75_exactly_seeks(self):
        action = syncplay.sync_action(local_position=98.25,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("seek", 100.0),
                         "diff == -1.75 hits the inclusive seek bound (§6.2)")

    def test_tempo_between_1_5_and_4(self):
        action = syncplay.sync_action(local_position=102.0,
                                      remote_position=100.0, paused=True,
                                      forward_delay=0.0)
        self.assertEqual(action, ("tempo", 0.95))

    def test_no_action_inside_drift_band(self):
        for local in (98.3, 99.0, 100.0, 101.4, 101.5):
            self.assertIsNone(
                syncplay.sync_action(local_position=local, remote_position=100.0,
                                     paused=True, forward_delay=0.0),
                "local=%r should be inside the band" % local)

    def test_playing_target_includes_forward_delay(self):
        # target = position + lastForwardDelay when not paused (§6.2)
        action = syncplay.sync_action(local_position=100.0,
                                      remote_position=100.0, paused=False,
                                      forward_delay=3.0)
        self.assertEqual(action, ("seek", 103.0),
                         "local - (remote+delay) = -3 -> seek to 103; "
                         "proves delay is in target")
        action = syncplay.sync_action(local_position=98.0,
                                      remote_position=100.0, paused=False,
                                      forward_delay=3.0)
        self.assertEqual(action, ("seek", 103.0),
                         "local - 103 = -5 -> seek to target 103")

    def test_paused_target_ignores_forward_delay(self):
        self.assertIsNone(syncplay.sync_action(local_position=100.0,
                                               remote_position=100.0, paused=True,
                                               forward_delay=3.0),
                          "paused target must ignore forward_delay")

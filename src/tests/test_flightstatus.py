import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skytrack import flightstatus as fs  # noqa: E402
from skytrack.live import State  # noqa: E402

WAW = SimpleNamespace(iata="WAW", lat=52.1657, lon=20.9671, tz="Europe/Warsaw")
NOW = datetime(2027, 6, 14, 4, 0, tzinfo=timezone.utc)
SCHED = NOW + timedelta(minutes=60)


def state(lat, lon, ground=False, speed=230.0, track=None, cs="LOT382"):
    return State(icao24="48ad01", callsign=cs, country="", lat=lat, lon=lon, alt_m=0 if ground else 10000,
                 on_ground=ground, speed_ms=speed, track=track, vrate=0, last_contact=0, reg="SP-LNA", type="E195")


class EvaluateTest(unittest.TestCase):
    def test_neighbours(self):
        self.assertEqual(fs.neighbour_numbers("LO 381"), ["LO 382", "LO 380"])
        self.assertEqual(fs.neighbour_numbers("FR0124"), ["FR 123", "FR 125"])
        self.assertEqual(fs.neighbour_numbers("hello"), [])

    def test_at_gate(self):
        st = fs.evaluate(SCHED, WAW, 1600, [state(52.17, 20.97, ground=True, speed=0)], [], NOW)
        self.assertEqual(st.state, "at_gate")
        self.assertEqual(st.delay_min, 0)

    def test_inbound_late(self):
        # ~250 km south-east of WAW (Kraków area), heading north-west at 830 km/h: lands in ~26 min,
        # + 35 min turnaround = ready ~61 min from now -> about on time; farther away -> delayed
        far = state(48.5, 25.0, track=320)
        st = fs.evaluate(SCHED, WAW, 1600, [], [("LO 382", far, "CFU", "WAW")], NOW)
        self.assertEqual(st.state, "inbound")
        self.assertGreater(st.delay_min, 15)
        self.assertEqual(st.inbound_from, "CFU")
        self.assertEqual(st.predicted_dep, st.inbound_eta + timedelta(minutes=fs.TURNAROUND_SHORT))

    def test_inbound_on_time(self):
        near = state(52.0, 21.3, track=300)
        st = fs.evaluate(SCHED, WAW, 1600, [], [("LO 382", near, "", "")], NOW)
        self.assertEqual(st.state, "inbound")
        self.assertEqual(st.delay_min, 0)
        self.assertEqual(st.predicted_dep, SCHED)

    def test_wrong_destination_ignored(self):
        st = fs.evaluate(SCHED, WAW, 1600, [], [("LO 382", state(50, 20, track=0), "CFU", "KRK")], NOW)
        self.assertEqual(st.state, "scheduled")

    def test_flying_away_ignored(self):
        away = state(50.0, 25.0, track=140)       # south-east of WAW, flying further south-east
        st = fs.evaluate(SCHED, WAW, 1600, [], [("LO 382", away, "", "")], NOW)
        self.assertEqual(st.state, "scheduled")

    def test_describe(self):
        st = fs.evaluate(SCHED, WAW, 1600, [], [("LO 382", state(48.5, 25.0, track=320), "CFU", "WAW")], NOW)
        badge, css, line = fs.describe(st, WAW)
        self.assertEqual(css, "warning")
        self.assertIn("CFU", line)


if __name__ == "__main__":
    unittest.main()

"""Run: python3 -m unittest discover -s tests   (from src/)"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SKYTRACK_LANG", "en")

from skytrack import bcbp  # noqa: E402
from skytrack.db import Trip  # noqa: E402

SIMPLE = "M1KOWALSKI/JAN MR     EABC123 WAWCFULO 0381 165Y012A0042 100"
# two legs; the first carries a conditional block with the date of issue (6164 = 2026, day 164)
COND1 = ">5180O 6164BLO 0000000000000"
TWO = ("M2NOWAK/ANNA          EXYZ789 KRKFRALH 1621 165C003D0007 1" + f"{len(COND1):02X}" + COND1 +
       "XYZ789 FRAJFKLH 0400 165C002A0011 100")


class ParseTest(unittest.TestCase):
    def test_simple(self):
        bp = bcbp.parse(SIMPLE, today=date(2026, 6, 1))
        self.assertEqual(bp.passenger, "Jan Kowalski")
        self.assertEqual(len(bp.legs), 1)
        leg = bp.legs[0]
        self.assertEqual((leg.pnr, leg.origin, leg.dest), ("ABC123", "WAW", "CFU"))
        self.assertEqual(leg.flight_no, "LO 381")
        self.assertEqual(leg.seat, "12A")
        self.assertEqual(leg.cabin, "economy")
        self.assertEqual(leg.flight_date, date(2026, 6, 14))

    def test_two_legs_with_issue_date(self):
        bp = bcbp.parse(TWO, today=date(2027, 1, 10))
        self.assertEqual(bp.issued, date(2026, 6, 13))
        self.assertEqual([(l.origin, l.dest) for l in bp.legs], [("KRK", "FRA"), ("FRA", "JFK")])
        self.assertEqual(bp.legs[0].cabin, "business")
        self.assertEqual(bp.legs[1].flight_no, "LH 400")
        # issue date wins over "nearest to today" (which would be 2027)
        self.assertEqual(bp.legs[1].flight_date, date(2026, 6, 14))

    def test_year_nearest(self):
        self.assertEqual(bcbp.julian_to_date(5, date(2026, 12, 20)), date(2027, 1, 5))
        self.assertEqual(bcbp.julian_to_date(350, date(2027, 1, 3)), date(2026, 12, 16))
        self.assertIsNone(bcbp.julian_to_date(0))
        # half a year ahead is no pass you can have yet: last year's trip
        self.assertEqual(bcbp.julian_to_date(165, date(2026, 11, 22)), date(2026, 6, 14))
        self.assertEqual(bcbp.julian_to_date(165, date(2026, 6, 1)), date(2026, 6, 14))
        self.assertEqual(bcbp.julian_to_date(165, date(2026, 3, 1)), date(2025, 6, 14))

    def test_not_a_pass(self):
        for junk in ("", "hello", "https://example.com/" + "x" * 80):
            with self.assertRaises(bcbp.BCBPError):
                bcbp.parse(junk)
        self.assertTrue(bcbp.looks_like_bcbp(SIMPLE))
        self.assertFalse(bcbp.looks_like_bcbp("WIFI:S:x;;"))

    def test_trips_and_merge(self):
        bp = bcbp.parse(TWO, today=date(2026, 6, 13))
        trips = bcbp.to_trips(bp, {"LH": ["DLH", "Lufthansa"]})
        self.assertEqual(trips[0].airline, "Lufthansa")
        self.assertIn("PNR XYZ789", trips[0].notes)
        self.assertEqual(len(trips[0].notes.split(" · ")), 2)
        log = [Trip(origin="KRK", dest="FRA", dep_date="2026-06-14", notes="window please", id=7)]
        hit = bcbp.find_match(log, trips[0])
        self.assertIs(hit, log[0])
        self.assertTrue(bcbp.merge_into(hit, trips[0]))
        self.assertEqual(hit.seat, "3D")
        self.assertTrue(hit.notes.startswith("window please · PNR XYZ789"))
        self.assertFalse(bcbp.merge_into(hit, trips[0]))     # second scan changes nothing

    def test_match_by_flight_number(self):
        log = [Trip(origin="WAW", dest="CFU", dep_date="2026-06-14", flight_no="LO0381"),
               Trip(origin="WAW", dest="XXX", dep_date="2026-06-14", flight_no="LO 3810")]
        scanned = Trip(origin="WMI", dest="CFU", dep_date="2026-06-14", flight_no="LO 381")
        self.assertIs(bcbp.find_match(log, scanned), log[0])


if __name__ == "__main__":
    unittest.main()

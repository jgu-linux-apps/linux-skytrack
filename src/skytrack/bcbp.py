"""IATA Bar Coded Boarding Pass (BCBP, Resolution 792) — the text inside the Aztec / PDF417 / QR code
on every boarding pass, e.g.

    M1KOWALSKI/JAN       EABC123 WAWCFULO 0381 165Y012A0042 100

Mandatory part: format 'M', number of legs, passenger name (20), e-ticket flag, then per leg 37 chars
(PNR 7, from 3, to 3, carrier 3, flight 5, day of the year 3, compartment 1, seat 4, check-in number 5,
status 1, hex size of the conditional block 2) followed by that conditional block. The code has no
year and no times: the year is taken from the pass's date of issue when the airline put it there,
otherwise from when check-in can open (see julian_to_date).
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

HEADER = 23
LEG = 37

CABINS = {
    "F": "first", "A": "first", "P": "first",
    "J": "business", "C": "business", "D": "business", "I": "business", "Z": "business", "R": "business",
    "W": "premium", "E": "premium", "T": "premium",
}


class BCBPError(ValueError):
    pass


@dataclass
class Leg:
    pnr: str
    origin: str
    dest: str
    carrier: str
    number: str
    julian: int
    compartment: str
    seat: str
    sequence: str
    flight_date: date | None = None

    @property
    def flight_no(self):
        return f"{self.carrier} {self.number}".strip()

    @property
    def cabin(self):
        """'economy', 'premium', 'business' or 'first' (airlines use the letters a bit freely)."""
        return CABINS.get(self.compartment.upper(), "economy")


@dataclass
class BoardingPass:
    name: str
    legs: list = field(default_factory=list)
    issued: date | None = None
    raw: str = ""

    @property
    def passenger(self):
        """'KOWALSKI/JAN MR' -> 'Jan Kowalski'."""
        last, _s, first = self.name.partition("/")
        first = re.sub(r"\s+(MR|MRS|MS|MISS|MSTR|DR|CHD|INF)$", "", first.strip())
        return " ".join(p.capitalize() for p in f"{first} {last}".split())


def _hex(s, where):
    try:
        return int(s, 16)
    except ValueError:
        raise BCBPError(f"bad field size at {where}: {s!r}") from None


CHECKIN_AHEAD_DAYS = 60       # online check-in opens at most about this long before the flight


def julian_to_date(day, today=None, issued=None):
    """Day of the year (1-366) -> date. With a date of issue: the first such day on or after it.
    Without it: a date up to CHECKIN_AHEAD_DAYS ahead (a pass you just got), otherwise the most recent
    past one (an old pass scanned for the log) — no pass exists for a flight half a year away."""
    today = today or date.today()
    if not 1 <= day <= 366:
        return None

    def make(y):
        d = date(y, 1, 1) + timedelta(days=day - 1)
        return d if d.year == y else None          # day 366 in a common year

    if issued:
        for y in (issued.year, issued.year + 1):
            d = make(y)
            if d and d >= issued:
                return d
    cands = [d for d in (make(today.year + 1), make(today.year), make(today.year - 1), make(today.year - 2)) if d]
    for d in cands:
        if today <= d <= today + timedelta(days=CHECKIN_AHEAD_DAYS):
            return d
    past = [d for d in cands if d < today]
    return max(past) if past else None


def _issue_date(s, today):
    """'6165' (last digit of the year + day of the year) -> date, not in the future."""
    if not re.fullmatch(r"\d{4}", s or ""):
        return None
    digit, day = int(s[0]), int(s[1:])
    if not 1 <= day <= 366:
        return None
    for back in range(0, 11):
        y = today.year - back
        if y % 10 == digit:
            d = date(y, 1, 1) + timedelta(days=day - 1)
            if d.year == y and d <= today + timedelta(days=1):
                return d
    return None


def looks_like_bcbp(text):
    return bool(re.match(r"^M[1-4].{21}[A-Z0-9 ]{7}[A-Z]{3}[A-Z]{3}", text or ""))


def parse(text, today=None):
    """BCBP string -> BoardingPass. Raises BCBPError when it is not a boarding pass."""
    today = today or date.today()
    s = (text or "").replace("\r", "").replace("\n", "")
    if len(s) < HEADER + LEG or s[0] != "M" or not s[1].isdigit():
        raise BCBPError("not a boarding pass")
    n = int(s[1])
    if not 1 <= n <= 4:
        raise BCBPError("bad number of legs")
    bp = BoardingPass(name=re.sub(r"\s+", " ", s[2:22]).strip(), raw=s)
    pos = HEADER
    legs = []
    for k in range(n):
        m = s[pos:pos + LEG]
        if len(m) < LEG - 2:
            raise BCBPError("truncated leg")
        if len(m) < LEG:                           # some passes drop the trailing '00' size
            m = m.ljust(LEG - 2) + "00"
        origin, dest = m[7:10].strip(), m[10:13].strip()
        if not (re.fullmatch(r"[A-Z]{3}", origin) and re.fullmatch(r"[A-Z]{3}", dest)):
            raise BCBPError("bad airport codes")
        num = m[16:21].strip()
        mm = re.fullmatch(r"0*(\d+)([A-Z]?)", num)
        number = (mm.group(1) + mm.group(2)) if mm else num
        day = m[21:24].strip()
        seat = m[25:29].strip()
        ms = re.fullmatch(r"0*(\d+[A-Z])", seat)
        legs.append(Leg(pnr=m[0:7].strip(), origin=origin, dest=dest, carrier=m[13:16].strip(),
                        number=number, julian=int(day) if day.isdigit() else 0,
                        compartment=m[24:25].strip(), seat=ms.group(1) if ms else seat.lstrip("0"),
                        sequence=m[29:34].strip().lstrip("0")))
        size = _hex(m[35:37], f"leg {k + 1}")
        cond = s[pos + LEG:pos + LEG + size]
        if k == 0 and cond.startswith(">") and len(cond) >= 4:
            usize = _hex(cond[2:4], "unique block")
            unique = cond[4:4 + usize]
            bp.issued = _issue_date(unique[3:7], today)
        pos += LEG + size
    for leg in legs:
        leg.flight_date = julian_to_date(leg.julian, today, bp.issued)
    bp.legs = legs
    return bp


def to_trips(bp, airlines_by_iata=None):
    """BoardingPass -> [db.Trip] (not saved). Notes carry the booking reference and a non-economy cabin."""
    from .db import Trip
    from .i18n import _
    cabin_names = {"premium": _('premium economy'), "business": _('biznes'), "first": _('pierwsza')}
    out = []
    for leg in bp.legs:
        notes = [f"PNR {leg.pnr}"] if leg.pnr else []
        if leg.cabin != "economy":
            notes.append(_('klasa {0}').format(cabin_names[leg.cabin]))
        airline = ""
        if airlines_by_iata:
            info = airlines_by_iata.get(leg.carrier)
            airline = info[1] if info else ""
        out.append(Trip(origin=leg.origin, dest=leg.dest,
                        dep_date=leg.flight_date.isoformat() if leg.flight_date else "",
                        flight_no=leg.flight_no, airline=airline, seat=leg.seat,
                        notes=" · ".join(notes)))
    return out


def merge_into(existing, scanned):
    """Copy what the pass knows onto a trip already in the log. Returns True when something changed."""
    changed = False
    for attr in ("flight_no", "seat", "airline"):
        v = getattr(scanned, attr)
        if v and getattr(existing, attr) != v:
            setattr(existing, attr, v)
            changed = True
    if scanned.notes:
        for part in scanned.notes.split(" · "):
            if part and part not in (existing.notes or ""):
                existing.notes = f"{existing.notes} · {part}" if existing.notes else part
                changed = True
    return changed


def _norm_flight(f):
    m = re.fullmatch(r"([A-Z0-9]{2})0*(\d+[A-Z]?)", re.sub(r"\s", "", (f or "").upper()))
    return m.group(1) + m.group(2) if m else re.sub(r"\s", "", (f or "").upper())


def find_match(trips, scanned):
    """A trip in the log that is the same flight: same day and route, or same day and flight number."""
    for t in trips:
        if t.dep_date != scanned.dep_date:
            continue
        if t.origin == scanned.origin and t.dest == scanned.dest:
            return t
        if t.flight_no and _norm_flight(t.flight_no) == _norm_flight(scanned.flight_no):
            return t
    return None

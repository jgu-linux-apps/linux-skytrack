"""Status of my next flights before departure, from free ADS-B data only (no airline API, no key).

Two signals:
  * my own flight's callsign already on air: the crew types it in ~30-40 min before departure, so the
    aircraft shows up at the gate ("at the gate"), then rolling or airborne ("departed");
  * the incoming aircraft: on most short-haul routes the plane flying my flight arrives just before as
    the paired flight with the neighbouring number (LO 382 CFU→WAW is flown by the LO 381 WAW→CFU plane,
    FR 1234 / FR 1235). When that flight is still in the air heading for my departure airport, its
    landing time + a minimum turnaround gives the earliest realistic departure: a delay forecast.

This is a forecast, not the airline's word: the gate and the official delay are not in ADS-B data.
"""
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from gi.repository import GLib, GObject

from . import geo
from .i18n import _

LOOK_AHEAD_H = 4            # start watching this long before the scheduled departure
KEEP_AFTER_MIN = 60         # and keep the status this long after it
DELAY_NOTIFY_MIN = 15
TURNAROUND_SHORT = 35       # minutes on the ground between landing and next departure (short haul)
TURNAROUND_LONG = 70        # flights over 3000 km
AT_GATE_KM = 4.0


@dataclass
class Status:
    state: str = "scheduled"          # scheduled | inbound | at_gate | departed | unknown
    sched_dep: datetime | None = None
    predicted_dep: datetime | None = None
    delay_min: int = 0
    inbound_label: str = ""           # "LO 381"
    inbound_from: str = ""            # IATA of where the incoming plane flies from
    inbound_eta: datetime | None = None
    reg: str = ""
    type: str = ""
    checked: float = 0.0
    error: str = ""
    events: set = field(default_factory=set)


def neighbour_numbers(flight_no):
    """'LO 381' -> ['LO 380', 'LO 382'] with the usual pair first (odd out, even back: 381 <-> 382)."""
    m = re.fullmatch(r"([A-Z0-9]{2})\s*0*(\d{1,4})", (flight_no or "").upper().strip())
    if not m:
        return []
    al, n = m.group(1), int(m.group(2))
    pair = n + 1 if n % 2 else n - 1
    other = n - 1 if n % 2 else n + 1
    return [f"{al} {x}" for x in (pair, other) if x > 0]


def turnaround_min(trip_km):
    return TURNAROUND_LONG if trip_km and trip_km > 3000 else TURNAROUND_SHORT


def eta_to(state, airport, now):
    """Landing time at airport for an airborne state (None when it is not heading there)."""
    from .live import _bearing
    if state.on_ground or state.lat is None:
        return None
    d = geo.distance_km(state.lat, state.lon, airport.lat, airport.lon)
    if d < 3:
        return now
    kmh = (state.speed_ms or 0) * 3.6
    kmh = kmh if kmh > 250 else 700
    return now + timedelta(minutes=d / kmh * 60 + (8 if d > 40 else 3))


def heading_to(state, airport, tolerance=35):
    from .live import _bearing
    if state.track is None:
        return True
    b = _bearing(state.lat, state.lon, airport.lat, airport.lon)
    diff = abs((state.track - b + 180) % 360 - 180)
    return diff <= tolerance or geo.distance_km(state.lat, state.lon, airport.lat, airport.lon) < 40


def evaluate(sched_dep, origin, trip_km, own, inbound, now):
    """Pure decision (testable). own: [State] seen under my callsign; inbound: [(label, State, from_iata or '',
    dest_iata or '')] — candidate incoming aircraft. Returns a Status (without events / checked)."""
    st = Status(sched_dep=sched_dep, predicted_dep=sched_dep)
    for s in own:
        d = geo.distance_km(s.lat, s.lon, origin.lat, origin.lon)
        if s.on_ground and d <= AT_GATE_KM:
            st.state, st.reg, st.type = "at_gate", s.reg, s.type
            if (s.speed_ms or 0) > 8:
                st.state = "departed"        # taxiing / take-off roll
            break
        if not s.on_ground and d < 400:
            st.state, st.reg, st.type = "departed", s.reg, s.type
            break
    if st.state in ("at_gate", "departed"):
        if st.state == "at_gate" and now > sched_dep:
            st.delay_min = int((now - sched_dep).total_seconds() // 60)
            st.predicted_dep = now + timedelta(minutes=5)
        return st
    best = None
    for label, s, frm, dest in inbound:
        if dest and dest != origin.iata:
            continue
        if not dest and not heading_to(s, origin):
            continue
        eta = eta_to(s, origin, now)
        if eta is None:
            continue
        if best is None or eta < best[1]:
            best = (label, eta, s, frm)
    if best is None:
        return st
    label, eta, s, frm = best
    ready = eta + timedelta(minutes=turnaround_min(trip_km))
    if ready - sched_dep > timedelta(hours=6):
        return st                           # not my plane after all (or a very different rotation)
    st.state = "inbound"
    st.inbound_label, st.inbound_from, st.inbound_eta = label, frm, eta
    st.reg, st.type = s.reg, s.type
    if ready > sched_dep:
        st.predicted_dep = ready
        st.delay_min = int((ready - sched_dep).total_seconds() // 60)
    return st


class StatusWatcher(GObject.Object):
    """Checks planned flights with a flight number and time that leave within LOOK_AHEAD_H hours."""

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "event": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str)),   # id, kind, title, body
    }

    def __init__(self, settings, airports, feed=None, route_lookup=None):
        super().__init__()
        from . import adsb
        self.settings, self.airports = settings, airports
        self.feed = feed or adsb.Feed()
        self.route_lookup = route_lookup or adsb.route_lookup
        self.status = {}                   # trip id -> Status
        self._busy = False

    def get(self, trip_id):
        return self.status.get(trip_id)

    def due(self, infos, now=None):
        """Infos worth checking now (and the scheduled departure of each)."""
        now = now or datetime.now(timezone.utc)
        out = []
        for i in infos:
            t = i.trip
            if not t.flight_no or not t.dep_time:
                continue
            dep = geo.local_dt(t.dep_date, t.dep_time, i.origin.tz)
            if dep is None:
                continue
            if dep - timedelta(hours=LOOK_AHEAD_H) <= now <= dep + timedelta(minutes=KEEP_AFTER_MIN):
                out.append((i, dep))
        return out

    def check(self, infos):
        due = self.due(infos)
        alive = {i.trip.id for i, _d in due}
        if any(k not in alive for k in self.status):
            self.status = {k: v for k, v in self.status.items() if k in alive}
            self.emit("changed")
        if not due or self._busy:
            return
        self._busy = True
        threading.Thread(target=self._work, args=(due,), daemon=True).start()

    # worker -------------------------------------------------------------------------------------
    def _states(self, flight_no):
        from .live import parse_query
        callsigns, _al, _l = parse_query(flight_no)
        seen = {}
        for cs in sorted(callsigns):
            try:
                for s in self.feed.by_callsign(cs):
                    seen[s.icao24] = s
            except Exception:
                continue
        return list(seen.values())

    def _work(self, due):
        results = {}
        now = datetime.now(timezone.utc)
        for info, dep in due:
            try:
                own = self._states(info.trip.flight_no)
                inbound = []
                if not own:
                    for label in neighbour_numbers(info.trip.flight_no):
                        for s in self._states(label):
                            if s.on_ground:
                                continue
                            frm = dest = ""
                            try:
                                from . import adsb
                                route = self.route_lookup(s.callsign, s.lat, s.lon)
                                a, b = adsb.resolve_route(route, self.airports, (s.lat, s.lon))
                                if a and b:
                                    frm, dest = a.iata, b.iata
                            except Exception:
                                pass
                            inbound.append((label, s, frm, dest))
                        if inbound:
                            break
                st = evaluate(dep, info.origin, info.km, own, inbound, now)
            except Exception as e:          # network trouble: keep the last good status
                st = Status(state="unknown", sched_dep=dep, predicted_dep=dep, error=str(e))
            st.checked = time.time()
            results[info.trip.id] = (info, st)
        GLib.idle_add(self._apply, results)

    def _apply(self, results):
        self._busy = False
        for tid, (info, st) in results.items():
            old = self.status.get(tid)
            if st.state == "unknown" and old is not None:
                old.error = st.error
                continue
            if old is not None:
                st.events = old.events
            self.status[tid] = st
            self._events(info, st)
        self.emit("changed")
        return False

    def _events(self, info, st):
        t = info.trip
        dep_local = lambda dt: dt.astimezone(_tz(info.origin)).strftime("%H:%M") if dt else "—"
        notified = list(self.settings["notified"] or [])

        def fire(kind, title, body):
            key = f"status:{t.id}:{t.dep_date}:{kind}"
            if key in notified or kind in st.events:
                return
            st.events.add(kind)
            notified.append(key)
            self.settings["notified"] = notified[-300:]
            self.emit("event", str(t.id), kind, title, body)

        if st.state == "inbound" and st.delay_min >= DELAY_NOTIFY_MIN:
            bucket = st.delay_min // 15 * 15          # tell again only when it grows by 15 min
            fire(f"delay{bucket}",
                 _('{0}: prognoza opóźnienia ok. {1} min').format(t.flight_no, st.delay_min),
                 _('Samolot ({0}) leci jeszcze{1}, lądowanie w {2} ok. {3}. Odlot najwcześniej ok. {4} zamiast {5}.').format(
                     st.inbound_label, _(' z {0}').format(st.inbound_from) if st.inbound_from else "",
                     t.origin, dep_local(st.inbound_eta), dep_local(st.predicted_dep), t.dep_time))
        elif st.state == "at_gate":
            fire("gate", _('{0}: samolot jest już w {1}').format(t.flight_no, t.origin),
                 _('Załoga wpisała już numer lotu. Planowy odlot {0}.').format(t.dep_time)
                 + (f" {st.type} {st.reg}".rstrip() if st.reg else ""))


def _tz(airport):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(airport.tz)
    except Exception:
        return timezone.utc


def describe(st, origin):
    """(badge text, css class, detail line) for the pass card."""
    def hm(dt):
        return dt.astimezone(_tz(origin)).strftime("%H:%M") if dt else "—"
    if st is None:
        return None
    if st.state == "departed":
        return _('Wystartował'), "success", (f"{st.type} · {st.reg}".strip(" ·") if st.reg else "")
    if st.state == "at_gate":
        extra = f" · {st.type} {st.reg}".rstrip() if st.reg else ""
        return _('Samolot na lotnisku'), "success", _('Załoga wpisała numer lotu') + extra
    if st.state == "inbound":
        where = _(' z {0}').format(st.inbound_from) if st.inbound_from else ""
        line = _('Samolot ({0}) leci teraz{1}, ląduje ok. {2}').format(st.inbound_label, where, hm(st.inbound_eta))
        if st.delay_min >= DELAY_NOTIFY_MIN:
            return (_('Prognoza +{0} min').format(st.delay_min), "warning",
                    line + " · " + _('odlot ok. {0}').format(hm(st.predicted_dep)))
        return _('Zgodnie z planem'), "success", line
    if st.state == "scheduled":
        return _('Brak danych z ADS-B'), "dim-label", _('Samolot jeszcze nie nadaje, sprawdzam co 5 min')
    return None

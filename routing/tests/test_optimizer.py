import json
import random
from collections import namedtuple
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from routing.services.corridor import StationIndex, route_miles, stations_along_route
from routing.services.optimizer import UnreachableError, plan_fuel, round_cents

S = namedtuple("S", "station_id mile_marker price")
FIXTURES = Path(__file__).parent / "fixtures"


def st(sid, mile, price):
    return S(sid, float(mile), Decimal(price))


def raw_cost(plan):
    return sum((s.gallons * s.price for s in plan.stops), Decimal(0))


class OptimizerTests(SimpleTestCase):
    def test_start_fuel_covers_trip(self):
        plan = plan_fuel([st(1, 100, "3.0")], 400, start_fuel_gallons=50)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, Decimal("0.00"))
        self.assertAlmostEqual(plan.fuel_remaining_at_finish, 10.0)

    def test_cheaper_station_ahead_buy_just_enough(self):
        plan = plan_fuel([st(1, 100, "4.0"), st(2, 300, "3.0")], 700, start_fuel_gallons=10)
        self.assertEqual([(s.station_id, s.gallons) for s in plan.stops],
                         [(1, Decimal("20.000")), (2, Decimal("40.000"))])

    def test_no_cheaper_in_range_fill_tank(self):
        plan = plan_fuel([st(1, 100, "3.0"), st(2, 300, "4.0")], 750, start_fuel_gallons=10)
        self.assertEqual([(s.station_id, s.gallons) for s in plan.stops],
                         [(1, Decimal("50.000")), (2, Decimal("15.000"))])
        self.assertEqual(plan.total_cost, Decimal("210.00"))

    def test_finish_reachable_buy_only_enough_and_arrive_empty(self):
        plan = plan_fuel([st(1, 100, "3.0")], 400, start_fuel_gallons=10)
        self.assertEqual([(s.station_id, s.gallons) for s in plan.stops], [(1, Decimal("30.000"))])
        self.assertAlmostEqual(plan.fuel_remaining_at_finish, 0.0, places=6)

    def test_gap_exactly_500_ok(self):
        plan = plan_fuel([st(1, 500, "3.0")], 1000, start_fuel_gallons=50)
        self.assertEqual([s.gallons for s in plan.stops], [Decimal("50.000")])

    def test_gap_over_500_unreachable(self):
        with self.assertRaises(UnreachableError) as ctx:
            plan_fuel([st(1, 500, "3.0")], 1000.01, start_fuel_gallons=50)
        e = ctx.exception
        self.assertAlmostEqual(e.gap_miles, 500.01)
        self.assertEqual((e.from_mile, e.to_mile), (500.0, 1000.01))

    def test_station_to_station_gap_unreachable(self):
        with self.assertRaises(UnreachableError) as ctx:
            plan_fuel([st(1, 100, "3.0"), st(2, 700, "3.0")], 900)
        self.assertEqual((ctx.exception.from_mile, ctx.exception.to_mile), (100.0, 700.0))

    def test_start_to_first_station_uses_start_fuel(self):
        with self.assertRaises(UnreachableError) as ctx:
            plan_fuel([st(1, 250, "3.0")], 600, start_fuel_gallons=20)
        self.assertEqual((ctx.exception.gap_miles, ctx.exception.from_mile, ctx.exception.to_mile), (250.0, 0.0, 250.0))

    def test_start_fuel_zero_without_station_at_mile_zero(self):
        with self.assertRaises(UnreachableError):
            plan_fuel([st(1, 3, "3.0")], 300, start_fuel_gallons=0)

    def test_start_fuel_zero_with_station_at_mile_zero(self):
        plan = plan_fuel([st(1, 0, "3.0")], 300, start_fuel_gallons=0)
        self.assertEqual([(s.station_id, s.gallons) for s in plan.stops], [(1, Decimal("30.000"))])

    def test_tie_prefers_farther(self):
        stations = [st(1, 100, "3.0"), st(2, 200, "3.5"), st(3, 400, "3.5")]
        plan = plan_fuel(stations, 900, start_fuel_gallons=10)
        self.assertEqual([(s.station_id, s.gallons) for s in plan.stops],
                         [(1, Decimal("50.000")), (3, Decimal("30.000"))])

    def test_zero_gallon_passes_not_in_output(self):
        plan = plan_fuel([st(1, 100, "3.5"), st(2, 200, "3.0")], 600, start_fuel_gallons=50)
        self.assertEqual([(s.station_id, s.gallons) for s in plan.stops], [(2, Decimal("10.000"))])

    def test_totals_and_rounding(self):
        stations = [st(1, 120.4567, "3.00733333"), st(2, 456.789, "3.41233333"), st(3, 777.7, "2.999")]
        plan = plan_fuel(stations, 1111.1111, start_fuel_gallons=12.3456)
        self.assertEqual(plan.total_cost, sum(s.cost for s in plan.stops))
        self.assertEqual(plan.total_gallons_purchased, sum(s.gallons for s in plan.stops))
        for s in plan.stops:
            self.assertEqual(s.gallons, s.gallons.quantize(Decimal("0.001")))
            self.assertEqual(s.cost, round_cents(s.gallons * s.price))
            self.assertLessEqual(abs(s.gallons * s.price - s.cost), Decimal("0.005"))
        self.assertAlmostEqual(plan.fuel_used_gallons, 111.11111)
        self.assertAlmostEqual(12.3456 + float(plan.total_gallons_purchased) - plan.fuel_used_gallons,
                               plan.fuel_remaining_at_finish, delta=0.002)

    def test_round_half_up(self):
        self.assertEqual(round_cents(Decimal("0.005")), Decimal("0.01"))
        self.assertEqual(round_cents(Decimal("2.345")), Decimal("2.35"))
        self.assertEqual(round_cents(Decimal("2.3449")), Decimal("2.34"))

    def test_stations_outside_route_ignored(self):
        plan = plan_fuel([st(1, -1, "1.0"), st(2, 100, "3.0"), st(3, 401, "1.0")], 400, start_fuel_gallons=10)
        self.assertEqual([s.station_id for s in plan.stops], [2])

    def test_invalid_arguments(self):
        for kwargs in ({"start_fuel_gallons": 51}, {"start_fuel_gallons": -1}, {"mpg": 0}, {"tank_gallons": 0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                plan_fuel([], 100, **kwargs)


def dp_optimum(stations, distance, tank=50, mpg=10, start_fuel=50):
    """Exact min cost over integer gallon levels; stations/distance are multiples of mpg miles."""
    nodes = [(0, None)] + [(int(s.mile_marker), s.price) for s in stations] + [(int(distance), None)]
    INF = None
    best = {start_fuel: Decimal(0)}  # fuel level on arrival at node k -> min cost
    for k in range(len(nodes) - 1):
        mile, price = nodes[k]
        need = (nodes[k + 1][0] - mile) // mpg
        after = {}
        for fuel, cost in best.items():
            buys = range(0, tank - fuel + 1) if price is not None else [0]
            for b in buys:
                if fuel + b >= need:
                    f2, c2 = fuel + b - need, cost + b * price if b else cost
                    if f2 not in after or c2 < after[f2]:
                        after[f2] = c2
        best = after
        if not best:
            return INF
    return min(best.values())


class BruteForceComparisonTests(SimpleTestCase):
    def test_greedy_matches_dp_on_random_cases(self):
        rng = random.Random(20260930)
        price_pool = ["2.899", "3.059", "3.199", "3.459", "3.999"]
        feasible = 0
        for case in range(200):
            distance = 10 * rng.randint(1, 120)
            stations = sorted(
                (st(i, 10 * rng.randint(0, distance // 10),
                    rng.choice(price_pool) if rng.random() < 0.5 else f"{rng.uniform(2.5, 4.5):.3f}")
                 for i in range(rng.randint(0, 8))),
                key=lambda s: (s.mile_marker, s.price, s.station_id))
            start_fuel = rng.randint(0, 50)
            expected = dp_optimum(stations, distance, start_fuel=start_fuel)
            with self.subTest(case=case, distance=distance, start_fuel=start_fuel, stations=stations):
                if expected is None:
                    with self.assertRaises(UnreachableError):
                        plan_fuel(stations, distance, start_fuel_gallons=start_fuel)
                    continue
                feasible += 1
                plan = plan_fuel(stations, distance, start_fuel_gallons=start_fuel)
                self.assertLessEqual(raw_cost(plan), expected + Decimal("0.01"))
                self.assertGreaterEqual(raw_cost(plan), expected - Decimal("0.01"))
        self.assertGreaterEqual(feasible, 50)


class FixtureRouteTests(SimpleTestCase):
    def test_ny_la_with_synthetic_stations_every_100_vertices(self):
        f = json.loads((FIXTURES / "ny_la_route.json").read_text())["features"][0]
        coords = f["geometry"]["coordinates"]
        rng = random.Random(1)
        picks = range(0, len(coords), 100)
        idx = StationIndex("t", list(picks), [coords[v][1] for v in picks], [coords[v][0] for v in picks],
                           [Decimal(f"{rng.uniform(2.8, 4.2):.3f}") for _ in picks])
        candidates = stations_along_route(coords, idx, corridor_miles=5)
        plan = plan_fuel(candidates, route_miles(coords)[-1])
        self.assertGreater(len(plan.stops), 0)
        self.assertEqual(plan.total_cost, sum(s.cost for s in plan.stops))
        self.assertAlmostEqual(plan.fuel_remaining_at_finish, 0.0, places=3)

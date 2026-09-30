"""Cheapest refuelling plan along a fixed route (pure functions, no Django).

Greedy "cheapest ahead": from each node, if a cheaper node is within a full tank's range, buy
just enough to reach the nearest one; otherwise fill up and move to the cheapest node in range.
Why it's optimal (exchange argument): any gallon burned before reaching a cheaper node is best
bought at the cheapest node already passed, so never carry more than needed into a cheaper
node, and when nothing ahead in range is cheaper, every gallon you can carry is cheapest here.
The finish is a virtual node with price 0, so the end of the trip needs no special case.
"""
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

EPS = 1e-9
GALLON_Q = Decimal("0.001")
CENT_Q = Decimal("0.01")


class UnreachableError(Exception):
    """The next node is beyond the reachable range; the API maps this to 422."""

    def __init__(self, gap_miles, from_mile, to_mile):
        self.gap_miles, self.from_mile, self.to_mile = gap_miles, from_mile, to_mile
        super().__init__(f"No fuel stop reachable: {gap_miles:.1f} mi gap from mile "
                         f"{from_mile:.1f} to mile {to_mile:.1f}.")


@dataclass(frozen=True)
class Stop:
    station_id: int
    mile_marker: float
    price: Decimal    # full precision
    gallons: Decimal  # 3 dp
    cost: Decimal     # cents, = round_cents(gallons × price)


@dataclass(frozen=True)
class FuelPlan:
    stops: list
    total_cost: Decimal
    total_gallons_purchased: Decimal
    fuel_used_gallons: float
    fuel_remaining_at_finish: float


@dataclass(frozen=True)
class _Node:
    mile: float
    price: float  # for comparisons only; math.inf at start, 0 at finish
    station: object = None


def round_cents(x):
    return x.quantize(CENT_Q, rounding=ROUND_HALF_UP)


def plan_fuel(stations, route_distance_miles, tank_gallons=50, mpg=10, start_fuel_gallons=50):
    """`stations`: objects with station_id, mile_marker, price (Decimal), sorted by mile_marker."""
    if tank_gallons <= 0 or mpg <= 0:
        raise ValueError("tank_gallons and mpg must be positive")
    if not 0 <= start_fuel_gallons <= tank_gallons:
        raise ValueError("start_fuel_gallons must be between 0 and tank_gallons")

    route_distance_miles = float(route_distance_miles)  # plain floats: numpy scalars break repr()->Decimal
    nodes = ([_Node(0.0, math.inf)]
             + [_Node(float(s.mile_marker), float(s.price), s) for s in stations
                if 0 <= s.mile_marker <= route_distance_miles]
             + [_Node(route_distance_miles, 0.0)])
    tank_range = tank_gallons * mpg

    i, fuel, purchases = 0, float(start_fuel_gallons), []
    while i < len(nodes) - 1:
        here = nodes[i]
        reach = here.mile + (fuel * mpg if i == 0 else tank_range)  # the start can't buy
        ahead = [j for j in range(i + 1, len(nodes)) if nodes[j].mile <= reach + EPS]
        if not ahead:
            nxt = nodes[i + 1]
            raise UnreachableError(nxt.mile - here.mile, here.mile, nxt.mile)

        cheaper = [j for j in ahead if nodes[j].price < here.price]
        if cheaper:
            j = cheaper[0]  # nearest cheaper node: buy only what it takes to get there
            buy = max(0.0, (nodes[j].mile - here.mile) / mpg - fuel)
        else:
            j = min(ahead, key=lambda k: (nodes[k].price, -nodes[k].mile))  # tie -> farther
            buy = tank_gallons - fuel

        if i == 0:
            buy = 0.0  # start price is +inf: j is the nearest node, already within start-fuel reach
        elif buy > EPS:
            purchases.append((here, buy))
        fuel = max(0.0, fuel + buy - (nodes[j].mile - here.mile) / mpg)
        i = j

    stops = []
    for node, gallons in purchases:
        g = Decimal(repr(gallons)).quantize(GALLON_Q, rounding=ROUND_HALF_UP)
        if g > 0:
            s = node.station
            stops.append(Stop(s.station_id, s.mile_marker, s.price, g, round_cents(g * s.price)))

    return FuelPlan(
        stops=stops,
        total_cost=sum((s.cost for s in stops), Decimal("0.00")),
        total_gallons_purchased=sum((s.gallons for s in stops), Decimal("0.000")),
        fuel_used_gallons=route_distance_miles / mpg,
        fuel_remaining_at_finish=round(fuel, 6),
    )

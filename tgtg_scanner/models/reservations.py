import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Dict, List
import time

from tgtg_scanner.models.item import Item
from tgtg_scanner.tgtg import TgtgClient
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("tgtg")

@dataclass
class Order:
    id: str
    item_id: str
    amount: int
    display_name: str

@dataclass
class Reservation:
    item_id: str
    amount: int
    display_name: str
    payment_url: str = ""
    pickup_time: str = ""

class Reservations:
    def __init__(self, client: TgtgClient) -> None:
        self.client = client
        self.reservation_query: list[Reservation] = []
        self.active_orders: dict[str, Order] = {}

    def reserve(self, item_id: str, display_name: str, amount: int = 1) -> None:
        """Create a new reservation.

        Args:
            item_id (str): Item ID
            display_name (str): Item display name
            amount (int, optional): Amount. Defaults to 1.

        """
        self.reservation_query.append(Reservation(item_id, amount, display_name))

    def make_orders(self, state: dict[str, Item], callback: Callable[[Reservation], None]) -> None:
        """Create orders for reservations.

        Args:
            state (Dict[str, Item]): Current item state
            callback (Callable[[Reservation], None]): Callback for each order

        """
        for reservation in self.reservation_query:
            item = state.get(reservation.item_id)
            if item and item.items_available > 0:
                try:
                    self._create_order(reservation)
                    self.reservation_query.remove(reservation)
                    callback(reservation)
                except Exception as exc:
                    log.warning("Order failed: %s", exc)

    def make_orders_spin(self, item_id: str) -> Reservation | None:
        """Create orders for reservations

        Args:
            item_id (str): Item ID
            callback (Callable[[Reservation], None]): Callback for each order
        """
        reserv = Reservation(item_id, 1, "spin")
        result = []
        def attempt_order():
            try:
                result.append(self._create_order(reserv))
            except Exception as exc:
                log.warning("Order failed: %s", exc)

        for attempt in range(4):
            threads = []
            for i in range(6):
                log.warning("thread is running: %s / 6 (attempt %s / 4)", i + 1, attempt + 1)
                thread = threading.Thread(target=attempt_order)
                thread.start()
                threads.append(thread)
                time.sleep(2)
            for thread in threads:
                thread.join()
            for res in result:
                if res is not None:
                    return res
        return None

    def update_active_orders(self) -> None:
        """Remove orders that are not active anymore."""
        for order_id in list(self.active_orders):
            res = self.client.get_order_status(order_id)
            if res.get("state") != "RESERVED":
                del self.active_orders[order_id]
            else:
                log.warning("orders: %s", res)

    def cancel_order(self, order_id: str) -> None:
        """Cancel an order."""
        self.client.abort_order(order_id)

    def cancel_all_orders(self) -> None:
        """Cancel all active orders."""
        for order_id in list(self.active_orders):
            self.cancel_order(order_id)

    def _create_order(self, reservation: Reservation) -> Reservation:
        res = self.client.create_order(reservation.item_id, reservation.amount)
        order_id = res.get("id")
        log.warning("new order %s", order_id)
        if order_id:
            order = Order(
                order_id,
                reservation.item_id,
                reservation.amount,
                reservation.display_name,
            )
            self.active_orders[order_id] = order
        try:
            item = self.client.get_item(reservation.item_id)
            PACIFIC = ZoneInfo("America/Los_Angeles")

            def parse_utc(ts: str) -> datetime:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))

            start_utc = parse_utc(item["pickup_interval"]["start"])
            end_utc = parse_utc(item["pickup_interval"]["end"])

            start_local = start_utc.astimezone(PACIFIC)
            end_local = end_utc.astimezone(PACIFIC)

            today_local = datetime.now(PACIFIC).date()

            def day_label(dt: datetime) -> str:
                if dt.date() == today_local:
                    return "Today"
                elif dt.date() == today_local + timedelta(days=1):
                    return "Tomorrow"
                else:
                    return dt.strftime("%A")  # fallback (Monday, Tuesday, etc.)

            label = day_label(start_local)

            start_time = start_local.strftime('%I:%M %p').lstrip('0')
            end_time = end_local.strftime('%I:%M %p').lstrip('0')
            time_string = f"{label}, {start_time} to {end_time} (PT)"
            print(time_string)
            reservation.pickup_time = time_string
            reservation.display_name = item["display_name"]
        except Exception as exc:
            log.warning("Failed to fetch item details for reservation: %s", exc)
            pass
        reservation.payment_url = self.client.pay_order(order_id)
        return reservation
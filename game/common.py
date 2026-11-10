"""Seat, bet, readiness, card and decision-timer helpers shared by every game table."""

from __future__ import annotations

import random
from collections import deque
from typing import Callable

from flask_socketio import emit


MIN_BET = 1
MAX_BET = 50_000
MAX_PLAYERS = 8
MIN_DECISION_SECONDS = 5
MAX_DECISION_SECONDS = 300


def error(text: str) -> None:
    """Send a notice to the client whose event is being handled."""
    emit("notice", {"text": text})


def register_handlers(socketio, handlers: dict[str, Callable]) -> None:
    for event_name, handler in handlers.items():
        socketio.on_event(event_name, handler)


def new_seat(account: dict, **game_fields) -> dict:
    """Account-backed fields every table keeps for a seated player, plus game-specific ones."""
    return {
        "account_id": account["id"],
        "account_type": account["account_type"],
        "profile_image_version": account["profile_image_version"],
        "is_admin": account["is_admin"],
        "name": account["username"],
        "credits": account["credits"],
        "bet": account["preferred_bet"],
        "connected": True,
        "ready": False,
        "round_credit_change": None,
        **game_fields,
    }


def seat_refusal(players: dict[str, dict], sid: str, account: dict) -> str | None:
    """Why this account can't take a seat, or None if it can."""
    if len(players) >= MAX_PLAYERS and sid not in players:
        return f"This table is full ({MAX_PLAYERS} players maximum)."
    if any(
        player["account_id"] == account["id"] and player["connected"]
        for other_sid, player in players.items() if other_sid != sid
    ):
        return "That account is already connected to this table."
    return None


def public_seat(sid: str, player: dict, accounts) -> dict:
    """The player fields every table shares with its clients."""
    return {
        "sid": sid,
        "name": player["name"],
        "account_type": player["account_type"],
        "profile_image_url": accounts.profile_image_url(
            player["account_id"], player.get("profile_image_version", 0)
        ),
        "credits": player["credits"],
        "bet": player["bet"],
        "connected": player["connected"],
        "ready": bool(player.get("ready")),
        "status": player["status"],
        "round_credit_change": player.get("round_credit_change"),
    }


def set_profile_image_version(players: dict[str, dict], sid: str, version: int) -> bool:
    player = players.get(sid)
    if player:
        player["profile_image_version"] = version
    return player is not None


def set_credit_balance(players: dict[str, dict], account_id: int, credits: int) -> list[dict]:
    """Apply an admin balance change; returns the seats it touched."""
    changed = [player for player in players.values() if player["account_id"] == account_id]
    for player in changed:
        player["credits"] = credits
    return changed


def parse_bet(data: dict | None, label: str = "bet") -> int | None:
    """Read a whole-number bet in the table limits, reporting problems to the client."""
    try:
        amount = int((data or {}).get("bet"))
    except (TypeError, ValueError):
        error(f"Enter a whole-number {label}.")
        return None
    if not MIN_BET <= amount <= MAX_BET:
        error(f"{label.capitalize()} must be a whole amount between {MIN_BET} and {MAX_BET:,} credits.")
        return None
    return amount


def save_bet_preference(app, accounts, player: dict, amount: int) -> bool:
    try:
        accounts.update_bet_preference(player["account_id"], amount)
    except Exception:
        app.logger.exception("Bet preference could not be saved")
        error("Could not save your bet. Please try again.")
        return False
    player["bet"] = amount
    return True


def everyone_ready(players: dict[str, dict], is_eligible: Callable[[dict], bool]) -> list[dict] | None:
    """The eligible seats if there is at least one and all of them are ready, else None."""
    eligible = [player for player in players.values() if is_eligible(player)]
    if eligible and all(player.get("ready") for player in eligible):
        return eligible
    return None


# --- Playing cards -------------------------------------------------------------

RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS = ("♠", "♥", "♦", "♣")
SUIT_LETTERS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANK_WORDS = {"ACE": "A", "JACK": "J", "QUEEN": "Q", "KING": "K", "TEN": "10"}


def card_rank(card: str) -> str:
    return card[:-1]


def card_suit(card: str) -> str:
    return card[-1]


def shuffled_cards(decks: int = 1) -> deque[str]:
    cards = [f"{rank}{suit}" for _ in range(decks) for suit in SUITS for rank in RANKS]
    random.shuffle(cards)
    return deque(cards)


def stack_card(cards: deque[str], spec: str) -> str | None:
    """Move the first card matching `rank[suit]` (e.g. A, 10H, K♠) to the top of the deck."""
    value = spec.strip().upper()
    suit = None
    if value and (value[-1] in SUIT_LETTERS or value[-1] in SUITS):
        suit = SUIT_LETTERS.get(value[-1], value[-1])
        value = value[:-1]
    value = RANK_WORDS.get(value, value)
    if value not in RANKS:
        return None
    card = next(
        (card for card in cards if card_rank(card) == value and (suit is None or card_suit(card) == suit)),
        None,
    )
    if card is None:
        return None
    cards.remove(card)
    cards.appendleft(card)
    return card


# --- Decision timer ------------------------------------------------------------

def parse_decision_seconds(value) -> int | None:
    """Read a per-decision time limit, reporting problems to the client."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = None
    if seconds is None or not MIN_DECISION_SECONDS <= seconds <= MAX_DECISION_SECONDS:
        error(f"Decision timer must be between {MIN_DECISION_SECONDS} and {MAX_DECISION_SECONDS} seconds.")
        return None
    return seconds


class DecisionTimer:
    """Countdown for the player currently deciding, with admin pause and override."""

    def __init__(self, seconds: int = 30):
        self.seconds = seconds
        self.remaining: int | None = None
        self.paused = False

    def reset(self, active: bool) -> None:
        self.remaining = self.seconds if active else None

    def tick(self) -> bool:
        """Count down one second; True when the current decision just ran out of time."""
        if self.remaining is None:
            self.remaining = self.seconds
        self.remaining = max(0, self.remaining - 1)
        return self.remaining == 0

    def public(self) -> dict:
        return {
            "round_decision_seconds": self.seconds,
            "timer_remaining": self.remaining,
            "timer_paused": self.paused,
            "min_decision_seconds": MIN_DECISION_SECONDS,
            "max_decision_seconds": MAX_DECISION_SECONDS,
        }

    def admin_command(self, parts: list[str], blocked: str | None = None) -> tuple[bool, str]:
        """Apply `/timer seconds|pause|resume`; `blocked` explains why seconds can't be set now."""
        usage = "Usage: /timer seconds|pause|resume"
        if len(parts) != 2:
            return False, usage
        value = parts[1].lower()
        if value == "pause":
            self.paused = True
            return True, "Decision timer paused."
        if value == "resume":
            self.paused = False
            return True, "Decision timer resumed."
        if not value.isdigit():
            return False, usage
        if blocked:
            return False, blocked
        seconds = int(value)
        if not 1 <= seconds <= 3600:
            return False, "Remaining timer time must be between 1 and 3,600 seconds."
        self.remaining = seconds
        return True, f"Decision timer set to {seconds} seconds remaining."


def run_decision_timer(socketio, lock, timer: DecisionTimer, is_waiting: Callable[[], bool],
                       on_expire: Callable[[], None], broadcast: Callable[[], None]) -> None:
    """Background loop: tick the timer while a player is deciding and expire their turn."""
    while True:
        socketio.sleep(1)
        with lock:
            if timer.paused or not is_waiting():
                continue
            if timer.tick():
                on_expire()
            broadcast()


# --- Manual draw ------------------------------------------------------------------

class ManualDraw:
    """Queue of table steps that deals cards, optionally pausing each draw for an admin.

    A step is a dict with a "kind"; steps marked "draws": False (bookkeeping such as
    "start the betting") run without a card. With manual draw on, the queue stops at the
    next card draw until an admin confirms it with /draw, so they can see (and stack)
    the top card first.
    """

    def __init__(self, deck: Callable[[], deque], run_step: Callable[[dict, str | None], None],
                 describe: Callable[[dict], str]):
        self._deck = deck
        self._run_step = run_step
        self._describe = describe
        self.enabled = False
        self.queue: deque[dict] = deque()
        self.pending: dict | None = None

    def add(self, *steps: dict) -> None:
        self.queue.extend(steps)

    def add_next(self, step: dict) -> None:
        self.queue.appendleft(step)

    def clear(self) -> None:
        self.queue.clear()
        self.pending = None

    def drop(self, predicate: Callable[[dict], bool]) -> None:
        """Remove queued steps (e.g. for a player who left)."""
        self.queue = deque(step for step in self.queue if not predicate(step))
        if self.pending is not None and predicate(self.pending):
            self.pending = None

    def pump(self) -> None:
        """Run queued steps until the queue empties or a manual draw needs confirming."""
        while self.queue:
            step = self.queue[0]
            draws = step.get("draws", True)
            if draws and self.enabled:
                self.pending = step
                return
            self.queue.popleft()
            self.pending = None
            self._run_step(step, self._deck().popleft() if draws else None)
        self.pending = None

    def disable(self) -> bool:
        """Turn manual draw off (e.g. when the admin leaves); True if it was on."""
        was_enabled = self.enabled
        self.enabled = False
        self.pending = None
        self.pump()
        return was_enabled

    def admin_command(self, command: str, parts: list[str]) -> tuple[bool, str] | None:
        """Handle /manualdraw on|off and /draw; None if the command is something else."""
        if command == "/manualdraw":
            if len(parts) != 2 or parts[1].lower() not in {"on", "off"}:
                return False, "Usage: /manualdraw on|off"
            if parts[1].lower() == "on":
                self.enabled = True
                self.pump()
            else:
                self.disable()
            return True, f"Manual draw mode {'enabled' if self.enabled else 'disabled'}."
        if command == "/draw":
            if len(parts) != 1 or not self.enabled or self.pending is None:
                return False, "There is no pending manual draw to confirm."
            step = self.queue.popleft()
            self.pending = None
            card = self._deck().popleft()
            self._run_step(step, card)
            self.pump()
            return True, f"Confirmed and drew {card}."
        return None

    def public(self, is_admin: bool) -> dict:
        """Manual-draw fields for a client's state; draw details only reach admins."""
        return {
            "manual_draw_enabled": self.enabled,
            "admin_draw_pending": bool(self.pending) if is_admin else False,
            "admin_draw_target": self._describe(self.pending) if is_admin and self.pending else "",
            "admin_deck_top": self._deck()[0] if is_admin and self.enabled and self._deck() else None,
        }

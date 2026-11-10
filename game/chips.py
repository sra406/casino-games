"""Chip-layout betting shared by the board games (roulette, craps).

Players stack chips on board spots. Each spot is a bet dict:
    {"kind", "selection", "description", "amount", "staged", ...game fields}
`amount` is everything on the spot and `staged` is the part placed since the table
last played, which has not been debited from the account yet. Staged chips can be
changed freely; when the table plays, `lock_staged` marks them debited. Games whose
bets stay up between rolls (craps) keep the locked part on the table.
"""

from __future__ import annotations

from typing import Callable

from game.common import everyone_ready


def chip_fields() -> dict:
    """Per-seat fields for a chip-layout table (pass to new_seat)."""
    return {"bets": [], "bet_actions": [], "previous_bets": [], "last_bets": []}


def staged_total(player: dict) -> int:
    return sum(bet["staged"] for bet in player["bets"])


def table_total(player: dict) -> int:
    return sum(bet["amount"] for bet in player["bets"])


def find_bet(player: dict, kind: str, selection: str) -> dict | None:
    return next((bet for bet in player["bets"] if bet["kind"] == kind and bet["selection"] == selection), None)


def add_chips(player: dict, kind: str, selection: str, description: str, amount: int,
              max_spots: int, **game_fields) -> str | None:
    """Stage `amount` more on a spot. Returns an error message, or None on success."""
    if staged_total(player) + amount > player["credits"]:
        return "Your new bets can't exceed your available credits."
    bet = find_bet(player, kind, selection)
    if bet:
        bet["amount"] += amount
        bet["staged"] += amount
    else:
        if len(player["bets"]) >= max_spots:
            return f"You can cover up to {max_spots} different spots."
        player["bets"].append({
            "kind": kind, "selection": selection, "description": description,
            "amount": amount, "staged": amount, **game_fields,
        })
    player["bet_actions"].append({"kind": kind, "selection": selection, "amount": amount})
    return None


def _take_staged(player: dict, bet: dict, amount: int) -> None:
    bet["amount"] -= amount
    bet["staged"] -= amount
    if bet["amount"] <= 0:
        player["bets"].remove(bet)


def undo_chips(player: dict) -> dict | None:
    """Take back the most recent chip placement; returns {"description", "amount"}."""
    while player["bet_actions"]:
        action = player["bet_actions"].pop()
        bet = find_bet(player, action["kind"], action["selection"])
        if bet and bet["staged"] > 0:
            amount = min(action["amount"], bet["staged"])
            _take_staged(player, bet, amount)
            return {"description": bet["description"], "amount": amount}
    return None


def remove_staged(player: dict, index: int) -> dict | None:
    """Remove the not-yet-played chips from one spot; returns the removed bet."""
    if not 0 <= index < len(player["bets"]):
        return None
    bet = player["bets"][index]
    if bet["staged"] <= 0:
        return None
    removed = {**bet, "amount": bet["staged"]}
    _take_staged(player, bet, bet["staged"])
    player["bet_actions"] = [
        action for action in player["bet_actions"]
        if (action["kind"], action["selection"]) != (bet["kind"], bet["selection"])
    ]
    return removed


def clear_staged(player: dict) -> int:
    """Remove every not-yet-played chip; returns the amount cleared."""
    total = 0
    for bet in list(player["bets"]):
        if bet["staged"]:
            total += bet["staged"]
            _take_staged(player, bet, bet["staged"])
    player["bet_actions"].clear()
    return total


def lock_staged(player: dict) -> int:
    """Mark staged chips as debited when the table plays; returns their total.

    The chips placed for this play become the seat's "rebet" layout.
    """
    staged = [bet for bet in player["bets"] if bet["staged"]]
    total = sum(bet["staged"] for bet in staged)
    if staged:
        player["previous_bets"] = [
            {"kind": bet["kind"], "selection": bet["selection"], "amount": bet["staged"]} for bet in staged
        ]
    for bet in staged:
        bet["staged"] = 0
    player["bet_actions"] = []
    return total


def rebet(player: dict, place: Callable[[str, str, int], str | None]) -> str | None:
    """Repeat the chips staged for the previous play through the game's own `place` check."""
    if staged_total(player):
        return "Clear your new chips before repeating your last bets."
    previous = player["previous_bets"]
    if not previous:
        return "There are no previous bets to repeat."
    total = sum(bet["amount"] for bet in previous)
    if total > player["credits"]:
        return f"Repeating your last bets needs {total:,} credits."
    for bet in previous:
        error = place(bet["kind"], bet["selection"], bet["amount"])
        if error:
            clear_staged(player)
            return error
    return None


def public_bets(player: dict) -> list[dict]:
    return [{**bet, "locked": bet["amount"] - bet["staged"]} for bet in player["bets"]]


def play_if_everyone_ready(players: dict[str, dict], is_eligible: Callable[[dict], bool],
                           has_bets: Callable[[dict], bool], begin: Callable[[], str | None],
                           nobody_bet_message: str) -> str | None:
    """Start the table once every eligible seat is ready and somebody has chips down.

    Returns a message for the table when it could not start, otherwise None.
    """
    eligible = everyone_ready(players, is_eligible)
    if not eligible:
        return None
    if not any(has_bets(player) for player in eligible):
        for player in eligible:
            player["ready"] = False
        return nobody_bet_message
    error = begin()
    if error:
        for player in players.values():
            player["ready"] = False
    return error


def reveal_later(socketio, lock, seconds: float, finish: Callable[[], None]) -> None:
    """Run `finish` under the table lock once an animation has had time to play."""
    def task():
        socketio.sleep(seconds)
        with lock:
            finish()

    socketio.start_background_task(task)

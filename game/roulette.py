"""Server-authoritative American roulette table and Socket.IO actions."""

from __future__ import annotations

import random
import time
import uuid

from flask_socketio import emit

from game import chips
from game.common import (
    MAX_BET, MIN_BET, error as _error, new_seat, parse_bet, public_seat,
    register_handlers, save_bet_preference, seat_refusal, set_credit_balance, set_profile_image_version,
)


MAX_BETS_PER_PLAYER = 40
SPIN_SECONDS = 7.0
HISTORY_LENGTH = 14

AMERICAN_WHEEL = [
    "0", "28", "9", "26", "30", "11", "7", "20", "32", "17", "5", "22",
    "34", "15", "3", "24", "36", "13", "1", "00", "27", "10", "25", "29",
    "12", "8", "19", "31", "18", "6", "21", "33", "16", "4", "23", "35", "14", "2",
]
RED_NUMBERS = {
    "1", "3", "5", "7", "9", "12", "14", "16", "18", "19", "21", "23",
    "25", "27", "30", "32", "34", "36",
}
NUMBER_VALUES = {str(number) for number in range(1, 37)} | {"0", "00"}

players: dict[str, dict] = {}
phase = "betting"  # betting, spinning, round_over
round_number = 0
message = "Place your bets, then spin the wheel."
last_result: dict | None = None
# While the wheel animates, the outcome is already settled in the database but
# only revealed to the table once the spin finishes.
current_spin: dict | None = None
pending_reveal: dict | None = None
history: list[dict] = []
# Admin-only override for the next spin's pocket ("0", "00" or "1"–"36").
forced_next_result: str | None = None

app = None
socketio = None
accounts = None
lock = None
request_sid = None


def register(app_instance, socketio_instance, account_service, shared_lock, request_sid_func) -> None:
    """Inject shared services and register this game's own socket events."""
    global app, socketio, accounts, lock, request_sid
    app = app_instance
    socketio = socketio_instance
    accounts = account_service
    lock = shared_lock
    request_sid = request_sid_func
    register_handlers(socketio, {
        "roulette_refresh_state": on_refresh_state,
        "roulette_set_bet": on_set_bet,
        "roulette_add_bet": on_add_bet,
        "roulette_remove_bet": on_remove_bet,
        "roulette_clear_bets": on_clear_bets,
        "roulette_undo_bet": on_undo_bet,
        "roulette_rebet": on_rebet,
        "roulette_toggle_ready": on_toggle_ready,
    })


def is_round_in_progress() -> bool:
    return phase == "spinning"


def attach_player(sid: str, account: dict) -> bool:
    with lock:
        if is_round_in_progress():
            _error("The roulette wheel is spinning. Join after the result.")
            return False
        refusal = seat_refusal(players, sid, account)
        if refusal:
            _error(refusal)
            return False
        players[sid] = new_seat(account, **chips.chip_fields(), status="no bets")
        _broadcast()
        return True


def update_profile_image_version(sid: str, version: int) -> None:
    with lock:
        if set_profile_image_version(players, sid, version):
            _broadcast()


def update_credit_balance(account_id: int, credits: int) -> None:
    with lock:
        if set_credit_balance(players, account_id, credits):
            _broadcast()
            _spin_if_everyone_ready()


def disconnect_player(sid: str) -> None:
    global message
    with lock:
        player = players.pop(sid, None)
        if player:
            message = f"{player['name']} left the roulette table."
            _broadcast()
            # The leaving player may have been the last one everyone was waiting on.
            _spin_if_everyone_ready()


def handle_admin_command(data: dict) -> None:
    global message, forced_next_result
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player:
            _error("Sign in with an admin account first.")
            return
        parts = str((data or {}).get("command", "")).strip().split()
        command = parts[0].lower() if parts else ""
        if command in {"/forceresult", "/force-result", "/forcenext"}:
            if len(parts) != 2:
                _error("Usage: /forceresult number|clear (number is 0, 00, or 1–36)")
                return
            value = parts[1].lower()
            if value in {"clear", "off", "random", "none"}:
                forced_next_result = None
                _notify(sid, "The next roulette result is random again.")
                _broadcast()
                return
            value = value if value == "00" else value.lstrip("0") or "0"
            if value not in NUMBER_VALUES:
                _error("Choose a pocket of 0, 00, or 1–36.")
                return
            forced_next_result = value
            note = " It applies to the spin after the current one." if phase == "spinning" else ""
            _notify(sid, f"The next spin will land on {value} {_color(value)}.{note}")
            _broadcast()
            return
        if command in {"/forcestart", "/force-start", "/forcespin", "/force-spin"}:
            if len(parts) != 1:
                _error("Usage: /forcestart")
                return
            if phase == "spinning":
                _error("The roulette wheel is already spinning.")
                return
            error = _begin_spin(f"Admin {player['name']} forced the wheel to spin. No more bets!")
            if error:
                _error(error)
            return
        _error(
            "Unknown roulette command. Use /forceresult number|clear, /forcestart, "
            "or /setcredits username amount."
        )


def _notify(sid: str, text: str) -> None:
    socketio.emit("notice", {"text": text}, to=sid)


def _color(number: str) -> str:
    if number in {"0", "00"}:
        return "green"
    return "red" if number in RED_NUMBERS else "black"


def _normalize_numbers(selection: str) -> list[str] | None:
    values = [value.strip() for value in selection.split("-")]
    if len(values) < 2 or len(values) > 6 or any(value not in NUMBER_VALUES for value in values):
        return None
    if len(set(values)) != len(values):
        return None
    return values


def _valid_split(numbers: list[str]) -> bool:
    if len(numbers) != 2:
        return False
    first, second = numbers
    if {first, second} == {"0", "00"}:
        return True
    if first in {"0", "00"} or second in {"0", "00"}:
        return (
            (first in {"0", "00"} and second in {"1", "2", "3"})
            or (second in {"0", "00"} and first in {"1", "2", "3"})
        )
    a, b = int(first), int(second)
    row_a, col_a = divmod(a - 1, 3)
    row_b, col_b = divmod(b - 1, 3)
    return (row_a == row_b and abs(col_a - col_b) == 1) or (col_a == col_b and abs(row_a - row_b) == 1)


def _valid_street(numbers: list[str]) -> bool:
    if len(numbers) != 3 or any(number in {"0", "00"} for number in numbers):
        return False
    values = sorted(int(number) for number in numbers)
    return values[0] % 3 == 1 and values == [values[0], values[0] + 1, values[0] + 2]


def _valid_corner(numbers: list[str]) -> bool:
    if len(numbers) != 4 or any(number in {"0", "00"} for number in numbers):
        return False
    values = {int(number) for number in numbers}
    for top_left in range(1, 33):
        row, column = divmod(top_left - 1, 3)
        if column < 2 and row < 11 and values == {top_left, top_left + 1, top_left + 3, top_left + 4}:
            return True
    return False


def _valid_sixline(numbers: list[str]) -> bool:
    if len(numbers) != 6 or any(number in {"0", "00"} for number in numbers):
        return False
    values = sorted(int(number) for number in numbers)
    return values[0] % 3 == 1 and values == list(range(values[0], values[0] + 6)) and values[0] <= 31


def _number_key(number: str) -> int:
    return {"0": -2, "00": -1}.get(number, 0) or int(number)


def _canonical_bet(kind: str, selection: str) -> tuple[str, str] | None:
    """Return (description, canonical selection) for a valid bet, else None."""
    if kind == "straight" and selection in NUMBER_VALUES:
        return selection, selection
    if kind in {"red", "black", "odd", "even", "low", "high", "topline"}:
        labels = {"topline": "Top line 0, 00, 1, 2, 3", "low": "1–18", "high": "19–36"}
        return labels.get(kind, kind.title()), ""
    if kind in {"dozen", "column"}:
        choices = {"1": "1st", "2": "2nd", "3": "3rd"}
        return (f"{choices[selection]} {kind}", selection) if selection in choices else None
    numbers = _normalize_numbers(selection)
    if not numbers:
        return None
    numbers = sorted(numbers, key=_number_key)
    canonical = "-".join(numbers)
    if kind == "split" and _valid_split(numbers):
        return f"Split {numbers[0]} / {numbers[1]}", canonical
    if kind == "street" and _valid_street(numbers):
        return f"Street {numbers[0]}–{numbers[-1]}", canonical
    if kind == "corner" and _valid_corner(numbers):
        return f"Corner {', '.join(numbers)}", canonical
    if kind == "sixline" and _valid_sixline(numbers):
        return f"Six line {numbers[0]}–{numbers[-1]}", canonical
    return None


# Total returned (stake included) per credit on a winning bet.
INSIDE_RETURNS = {"straight": 36, "split": 18, "street": 12, "corner": 9, "sixline": 6, "topline": 7}
EVEN_MONEY = {
    "red": lambda n: str(n) in RED_NUMBERS,
    "black": lambda n: str(n) not in RED_NUMBERS,
    "odd": lambda n: n % 2 == 1,
    "even": lambda n: n % 2 == 0,
    "low": lambda n: n <= 18,
    "high": lambda n: n >= 19,
}


def _bet_payout(bet: dict, result: str) -> int:
    kind, selection, amount = bet["kind"], bet["selection"], bet["amount"]
    if kind in INSIDE_RETURNS:
        covered = {"0", "00", "1", "2", "3"} if kind == "topline" else set(selection.split("-"))
        return amount * INSIDE_RETURNS[kind] if result in covered else 0
    if result in {"0", "00"}:
        return 0  # every outside bet loses on the zeros
    number = int(result)
    if kind == "dozen":
        return amount * 3 if str((number - 1) // 12 + 1) == selection else 0
    if kind == "column":
        return amount * 3 if str((number - 1) % 3 + 1) == selection else 0
    wins = EVEN_MONEY.get(kind)
    return amount * 2 if wins and wins(number) else 0


def _public_state(for_sid: str) -> dict:
    own_player = players.get(for_sid, {})
    spin = None
    if phase == "spinning" and current_spin:
        elapsed = time.monotonic() - current_spin["started_at"]
        spin = {
            "id": current_spin["id"],
            "number": current_spin["number"],
            "color": current_spin["color"],
            "duration_ms": int(SPIN_SECONDS * 1000),
            "remaining_ms": max(0, int((SPIN_SECONDS - elapsed) * 1000)),
        }
    return {
        "phase": phase,
        "own_sid": for_sid,
        "own_is_admin": bool(own_player.get("is_admin")),
        "round_number": round_number,
        "message": message,
        "last_result": dict(last_result) if last_result else None,
        "spin": spin,
        "history": [dict(entry) for entry in history],
        "forced_next_result": forced_next_result if own_player.get("is_admin") else None,
        "players": [
            {
                **public_seat(sid, player, accounts),
                "bets": chips.public_bets(player),
                "last_bets": [dict(bet) for bet in player["last_bets"]],
                "can_rebet": bool(player["previous_bets"]),
                "can_undo": bool(player["bet_actions"]),
                "ready_eligible": _ready_eligible(player),
            }
            for sid, player in players.items()
        ],
        "min_bet": MIN_BET,
        "max_bet": MAX_BET,
        "max_bets_per_player": MAX_BETS_PER_PLAYER,
    }


def _broadcast() -> None:
    for sid, player in list(players.items()):
        if player["connected"]:
            socketio.emit("roulette_state", _public_state(sid), to=sid)


def _player_for_betting(sid: str) -> dict | None:
    player = players.get(sid)
    if not player:
        _error("Join the roulette table first.")
        return None
    if phase == "spinning":
        _error("No more bets — the wheel is spinning.")
        return None
    return player


def _ready_eligible(player: dict) -> bool:
    """Seats the spin waits on: anyone with chips down, or credits to place some."""
    return player["connected"] and (bool(player["bets"]) or player["credits"] >= MIN_BET)


def _bets_changed(player: dict) -> None:
    """Changing bets cancels readiness so nobody is spun on an unconfirmed layout."""
    player["ready"] = False
    player["status"] = "bets placed" if player["bets"] else "no bets"


def _start_new_bets(player: dict) -> None:
    """Clear the previous spin's result display once a player starts betting again."""
    if player["last_bets"]:
        player["last_bets"] = []
        player["round_credit_change"] = None


def on_refresh_state(_data=None):
    with lock:
        sid = request_sid()
        if sid in players and players[sid]["connected"]:
            emit("roulette_state", _public_state(sid), to=sid)


def on_set_bet(data):
    with lock:
        player = players.get(request_sid())
        if not player:
            _error("Join the roulette table first.")
            return
        amount = parse_bet(data, "chip value")
        if amount is not None and save_bet_preference(app, accounts, player, amount):
            _broadcast()


def on_add_bet(data):
    global message
    with lock:
        sid = request_sid()
        player = _player_for_betting(sid)
        if not player:
            return
        kind = str((data or {}).get("kind", "")).strip().lower()
        selection = str((data or {}).get("selection", "")).strip()
        error = _place(player, kind, selection, player["bet"])
        if error:
            _error(error)
            return
        _start_new_bets(player)
        _bets_changed(player)
        message = f"{player['name']} bet {player['bet']:,} on {_canonical_bet(kind, selection)[0]}."
        _broadcast()


def _place(player: dict, kind: str, selection: str, amount: int) -> str | None:
    canonical = _canonical_bet(kind, selection)
    if canonical is None:
        return "Choose a valid roulette bet."
    description, selection = canonical
    return chips.add_chips(player, kind, selection, description, amount, MAX_BETS_PER_PLAYER)


def on_remove_bet(data):
    global message
    with lock:
        sid = request_sid()
        player = _player_for_betting(sid)
        if not player:
            return
        try:
            index = int((data or {}).get("index"))
        except (TypeError, ValueError):
            return
        removed = chips.remove_staged(player, index)
        if removed:
            _bets_changed(player)
            message = f"{player['name']} removed {removed['description']}."
            _broadcast()


def on_undo_bet(_data=None):
    global message
    with lock:
        sid = request_sid()
        player = _player_for_betting(sid)
        undone = chips.undo_chips(player) if player else None
        if not undone:
            return
        message = f"{player['name']} took back {undone['amount']:,} from {undone['description']}."
        _bets_changed(player)
        _broadcast()


def on_clear_bets(_data=None):
    global message
    with lock:
        sid = request_sid()
        player = _player_for_betting(sid)
        if not player:
            return
        chips.clear_staged(player)
        _bets_changed(player)
        message = f"{player['name']} cleared their bets."
        _broadcast()


def on_rebet(_data=None):
    global message
    with lock:
        sid = request_sid()
        player = _player_for_betting(sid)
        if not player:
            return
        error = chips.rebet(player, lambda kind, selection, amount: _place(player, kind, selection, amount))
        if error:
            _error(error)
            return
        total = chips.staged_total(player)
        _start_new_bets(player)
        _bets_changed(player)
        message = f"{player['name']} repeated their last bets ({total:,} credits)."
        _broadcast()


def on_toggle_ready(_data=None):
    """Toggle the requesting player's readiness; spin once every seat is ready."""
    global message
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player or not player["connected"]:
            _error("Join the roulette table first.")
            return
        if phase == "spinning":
            _error("The roulette wheel is already spinning.")
            return
        player["ready"] = not player["ready"]
        if player["ready"]:
            message = (
                f"{player['name']} is ready." if player["bets"]
                else f"{player['name']} is sitting out this spin."
            )
        else:
            message = f"{player['name']} is no longer ready."
        _broadcast()
        _spin_if_everyone_ready()


def _spin_if_everyone_ready() -> None:
    """Start the spin when every eligible seat is ready and someone has chips down."""
    global message
    if phase == "spinning":
        return
    notice = chips.play_if_everyone_ready(
        players, _ready_eligible, lambda player: bool(player["bets"]),
        lambda: _begin_spin("Everyone is ready. No more bets!"),
        "Everyone sat out, so the wheel did not spin. Place bets and ready up.",
    )
    if notice:
        message = notice
        _broadcast()


def _begin_spin(start_message: str) -> str | None:
    """Debit every live bet, settle the result and start the wheel. Returns an error, if any."""
    global phase, round_number, message, current_spin, pending_reveal, forced_next_result
    participants = [
        (player_sid, player) for player_sid, player in players.items()
        if player["connected"] and player["bets"]
    ]
    if not participants:
        return "Nobody has placed a bet, so the wheel can't spin."
    round_id = uuid.uuid4().hex
    wagers = [(player["account_id"], chips.staged_total(player)) for _, player in participants]
    try:
        balances = accounts.debit_round_bets(round_id, wagers)
    except ValueError:
        return "A player has insufficient credits for their bets."
    except Exception:
        app.logger.exception("Could not debit roulette bets")
        return "Could not spin because account storage failed."

    phase = "spinning"
    round_number += 1
    if forced_next_result is not None:
        result_number = forced_next_result
        forced_next_result = None
    else:
        result_number = random.choice(AMERICAN_WHEEL)
    result_color = _color(result_number)
    reveals: dict[str, dict] = {}
    payouts_by_account: dict[int, int] = {}
    for player in players.values():
        player["ready"] = False
    for player_sid, player in participants:
        player["credits"] = balances[player["account_id"]]
        total_wager = chips.lock_staged(player)
        round_bets = []
        total_payout = 0
        for bet in player["bets"]:
            payout = _bet_payout(bet, result_number)
            total_payout += payout
            round_bets.append({**bet, "payout": payout, "won": payout > 0})
        player["status"] = "locked"
        reveals[player_sid] = {
            "last_bets": round_bets,
            "round_credit_change": total_payout - total_wager,
            "status": "won" if total_payout else "lost",
        }
        payouts_by_account[player["account_id"]] = total_payout

    settlement_failed = False
    settled_balances: dict[int, int] = {}
    try:
        settled_balances = accounts.settle_round_payouts(
            round_id,
            [(account_id, payout) for account_id, payout in payouts_by_account.items()],
        )
    except Exception:
        app.logger.exception("Roulette payout settlement failed")
        settlement_failed = True

    current_spin = {
        "id": round_number,
        "number": result_number,
        "color": result_color,
        "started_at": time.monotonic(),
    }
    pending_reveal = {
        "round_number": round_number,
        "players": reveals,
        "balances": settled_balances,
        "failed": settlement_failed,
    }
    message = start_message
    _broadcast()
    expected_round = round_number
    chips.reveal_later(socketio, lock, SPIN_SECONDS, lambda: _finish_spin(expected_round))
    return None


def _finish_spin(expected_round: int) -> None:
    """Reveal the settled result once the wheel animation has had time to play."""
    global phase, message, last_result, current_spin, pending_reveal
    if phase != "spinning" or not current_spin or current_spin["id"] != expected_round:
        return
    reveal = pending_reveal or {"players": {}, "balances": {}, "failed": False}
    for player_sid, outcome in reveal["players"].items():
        player = players.get(player_sid)
        if not player:
            continue
        player["bets"] = []
        player["last_bets"] = outcome["last_bets"]
        player["round_credit_change"] = outcome["round_credit_change"]
        player["status"] = outcome["status"]
    for player in players.values():
        if player["account_id"] in reveal["balances"]:
            player["credits"] = reveal["balances"][player["account_id"]]
    result_number, result_color = current_spin["number"], current_spin["color"]
    last_result = {
        "number": result_number,
        "color": result_color,
        "round_number": current_spin["id"],
    }
    history.insert(0, {"number": result_number, "color": result_color})
    del history[HISTORY_LENGTH:]
    phase = "round_over"
    current_spin = None
    pending_reveal = None
    if reveal["failed"]:
        message = (
            f"The wheel landed on {result_number} {result_color}, but credits could not be settled. "
            "The pending wagers will be refunded at server restart."
        )
    else:
        message = f"The wheel landed on {result_number} {result_color}. Place bets for the next spin."
    _broadcast()

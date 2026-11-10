"""Server-authoritative casino craps table and Socket.IO actions.

Standard rules: pass/don't pass with bar-12, come/don't come, free odds (3-4-5x;
lay odds up to 6x the flat bet), place bets, field (2 pays double, 12 pays triple),
Big 6/8, hardways and one-roll propositions. Place bets, hardways and come-bet odds
are off on the come-out roll.

Chips use the shared chip layout (game/chips.py). Bets that stay up between rolls
remain debited and are recorded under the table's round id, so a server restart
refunds them like any other unsettled wager.
"""

from __future__ import annotations

import random
import time
import uuid

from flask_socketio import emit

from game import chips
from game.common import (
    MAX_BET, MIN_BET, error as _error, everyone_ready, new_seat, parse_bet, public_seat, register_handlers,
    save_bet_preference, seat_refusal, set_credit_balance, set_profile_image_version,
)


MAX_SPOTS_PER_PLAYER = 40
ROLL_SECONDS = 2.6
HISTORY_LENGTH = 16
POINTS = (4, 5, 6, 8, 9, 10)
# Winnings per unit staked, as (numerator, denominator).
TRUE_ODDS = {4: (2, 1), 10: (2, 1), 5: (3, 2), 9: (3, 2), 6: (6, 5), 8: (6, 5)}
PLACE_PAYS = {4: (9, 5), 10: (9, 5), 5: (7, 5), 9: (7, 5), 6: (7, 6), 8: (7, 6)}
HARDWAY_PAYS = {4: 7, 10: 7, 6: 9, 8: 9}
MAX_ODDS = {4: 3, 10: 3, 5: 4, 9: 4, 6: 5, 8: 5}  # 3-4-5x odds on the flat bet
MAX_LAY_ODDS = 6  # lay odds up to 6x pay at most the 3-4-5x win
PROPOSITIONS = {  # one-roll bets: winning totals and pays-to-one
    "any7": ({7}, 4, "Any seven"),
    "anycraps": ({2, 3, 12}, 7, "Any craps"),
    "two": ({2}, 30, "Aces (2)"),
    "three": ({3}, 15, "Ace-deuce (3)"),
    "yo": ({11}, 15, "Yo (11)"),
    "twelve": ({12}, 30, "Boxcars (12)"),
}
FIELD_PAYS = {2: 2, 3: 1, 4: 1, 9: 1, 10: 1, 11: 1, 12: 3}
# Contract bets can't be taken down once a point is working for them.
CONTRACT_KINDS = {"pass", "come"}

# Bets stay on the table across rolls, so the whole table shares one refund record.
TABLE_ROUND_ID = uuid.uuid4().hex

players: dict[str, dict] = {}
phase = "betting"  # betting, rolling
point: int | None = None
shooter_sid: str | None = None
roll_number = 0
message = "Place your bets on the come-out roll."
last_roll: dict | None = None
current_roll: dict | None = None
pending_reveal: dict | None = None
history: list[dict] = []
forced_next_roll: tuple[int, int] | None = None

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
        "craps_refresh_state": on_refresh_state,
        "craps_set_bet": on_set_bet,
        "craps_add_bet": on_add_bet,
        "craps_remove_bet": on_remove_bet,
        "craps_clear_bets": on_clear_bets,
        "craps_undo_bet": on_undo_bet,
        "craps_rebet": on_rebet,
        "craps_toggle_ready": on_toggle_ready,
        "craps_roll": on_roll,
    })


def is_round_in_progress() -> bool:
    return phase == "rolling"


# --- Account adapter hooks -------------------------------------------------------

def attach_player(sid: str, account: dict) -> bool:
    global message, shooter_sid
    with lock:
        if is_round_in_progress():
            _error("The dice are rolling. Join after this roll.")
            return False
        refusal = seat_refusal(players, sid, account)
        if refusal:
            _error(refusal)
            return False
        players[sid] = new_seat(account, **chips.chip_fields(), status="no bets")
        if shooter_sid not in players:
            shooter_sid = sid
        message = f"{account['username']} joined the craps table."
        _broadcast()
        return True


def update_profile_image_version(sid: str, version: int) -> None:
    with lock:
        if set_profile_image_version(players, sid, version):
            _broadcast()


def update_credit_balance(account_id: int, credits: int) -> None:
    with lock:
        changed = set_credit_balance(players, account_id, credits)
        for player in changed:
            if chips.staged_total(player) > credits:
                player["ready"] = False
        if changed:
            _broadcast()
            _roll_if_everyone_ready()


def disconnect_player(sid: str) -> None:
    """Leaving takes every bet down except contract bets with a point, which are lost."""
    global message
    with lock:
        player = players.pop(sid, None)
        if not player:
            return
        if phase == "rolling" and pending_reveal and sid in pending_reveal["players"]:
            player["bets"] = pending_reveal["players"][sid]["bets"]
        refund = forfeit = 0
        for bet in player["bets"]:
            locked = bet["amount"] - bet["staged"]
            if _removable(bet):
                refund += locked
            else:
                forfeit += locked
        if refund or forfeit:
            try:
                accounts.return_round_wager(TABLE_ROUND_ID, player["account_id"], refund, forfeit)
            except Exception:
                app.logger.exception("Could not return craps bets for a departing player")
        _pass_dice_if_shooter(sid)
        message = f"{player['name']} left the craps table."
        _broadcast()
        _roll_if_everyone_ready()


def handle_admin_command(data: dict) -> None:
    global forced_next_roll
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player:
            _error("Sign in with an admin account first.")
            return
        parts = str((data or {}).get("command", "")).strip().split()
        command = parts[0].lower() if parts else ""
        if command in {"/forceroll", "/force-roll", "/forceresult"}:
            if len(parts) == 2 and parts[1].lower() in {"clear", "off", "random", "none"}:
                forced_next_roll = None
                _notify(sid, "The next roll is random again.")
                _broadcast()
                return
            dice = _parse_dice(parts[1:])
            if dice is None:
                _error("Usage: /forceroll die1 die2 (for example /forceroll 3 4 or /forceroll 6-6) or /forceroll clear")
                return
            forced_next_roll = dice
            note = " It applies to the roll after this one." if phase == "rolling" else ""
            _notify(sid, f"The next roll will be {dice[0]}-{dice[1]} ({sum(dice)}).{note}")
            _broadcast()
            return
        if command in {"/forcestart", "/force-start", "/forceroll-now", "/roll"}:
            if len(parts) != 1:
                _error("Usage: /forcestart")
                return
            if phase == "rolling":
                _error("The dice are already rolling.")
                return
            error = _begin_roll(f"Admin {player['name']} rolled the dice.")
            if error:
                _error(error)
            return
        _error("Craps commands: /forceroll die1 die2|clear · /forcestart · /setcredits username amount")


def _parse_dice(values: list[str]) -> tuple[int, int] | None:
    text = " ".join(values).replace("-", " ").replace(",", " ").split()
    if len(text) != 2 or not all(value.isdigit() for value in text):
        return None
    dice = int(text[0]), int(text[1])
    return dice if all(1 <= die <= 6 for die in dice) else None


def _notify(sid: str, text: str) -> None:
    socketio.emit("notice", {"text": text}, to=sid)


def _pass_dice_if_shooter(sid: str) -> None:
    """Give the dice to the first remaining seat when the shooter leaves."""
    global shooter_sid
    if shooter_sid == sid or shooter_sid not in players:
        shooter_sid = next(iter(players), None)


def _next_shooter() -> None:
    global shooter_sid
    order = [seat for seat, player in players.items() if player["connected"]]
    if not order:
        shooter_sid = None
    elif shooter_sid in order:
        shooter_sid = order[(order.index(shooter_sid) + 1) % len(order)]
    else:
        shooter_sid = order[0]


# --- Bet rules ---------------------------------------------------------------------

def _point_name(number: int) -> str:
    return {6: "Six", 9: "Nine"}.get(number, str(number))


def _describe(kind: str, selection: str) -> str:
    if kind in PROPOSITIONS:
        return PROPOSITIONS[kind][2]
    return {
        "pass": "Pass line", "dontpass": "Don't pass", "pass_odds": "Pass odds", "dontpass_odds": "Don't pass odds",
        "come": f"Come {selection}" if selection else "Come", "dontcome": f"Don't come {selection}" if selection else "Don't come",
        "come_odds": f"Come odds on {selection}", "dontcome_odds": f"Don't come odds on {selection}",
        "place": f"Place {selection}", "hard": f"Hard {selection}", "big": f"Big {selection}", "field": "Field",
    }.get(kind, kind)


def _removable(bet: dict) -> bool:
    """Whether the debited part of a bet can be taken down between rolls."""
    return bet["kind"] not in CONTRACT_KINDS


def _validate(player: dict, kind: str, selection: str, amount: int) -> tuple[str, str] | str:
    """(kind, selection) to stage chips on, or an error message."""
    number = int(selection) if selection.isdigit() else None
    if kind in {"pass", "dontpass"}:
        if point is not None:
            return "Line bets go down on the come-out roll. Use come or don't come while a point is on."
        return kind, ""
    if kind in {"come", "dontcome"}:
        if point is None:
            return "Come bets are made once a point is on. Use the pass or don't pass line now."
        return kind, ""
    if kind in {"pass_odds", "dontpass_odds"}:
        base_kind = kind.removesuffix("_odds")
        base = chips.find_bet(player, base_kind, "")
        if point is None or not base or base["amount"] - base["staged"] <= 0:
            return f"Odds go behind a {_describe(base_kind, '').lower()} bet once the point is set."
        return _check_odds(player, kind, "", base, point, amount)
    if kind == "odds":  # the odds strip on a number box: odds for your come / don't come bet there
        if number not in POINTS:
            return "Choose a point number."
        for base_kind in ("come", "dontcome"):
            base = chips.find_bet(player, base_kind, selection)
            if base:
                return _check_odds(player, f"{base_kind}_odds", selection, base, number, amount)
        return f"Put a come or don't come bet on {number} first; it moves there when {number} rolls."
    if kind == "place":
        return (kind, selection) if number in POINTS else "Place bets go on 4, 5, 6, 8, 9 or 10."
    if kind == "hard":
        return (kind, selection) if number in HARDWAY_PAYS else "Hardways are 4, 6, 8 and 10."
    if kind == "big":
        return (kind, selection) if number in (6, 8) else "Choose Big 6 or Big 8."
    if kind == "field" or kind in PROPOSITIONS:
        return kind, ""
    return "Choose a valid craps bet."


def _check_odds(player: dict, kind: str, selection: str, base: dict, number: int, amount: int):
    existing = chips.find_bet(player, kind, selection)
    limit = base["amount"] * (MAX_LAY_ODDS if kind.startswith("dont") else MAX_ODDS[number])
    if (existing["amount"] if existing else 0) + amount > limit:
        return f"Odds on {number} are limited to {limit:,} for this bet."
    return kind, selection


def _place(player: dict, kind: str, selection: str, amount: int) -> str | None:
    checked = _validate(player, kind, selection, amount)
    if isinstance(checked, str):
        return checked
    kind, selection = checked
    return chips.add_chips(player, kind, selection, _describe(kind, selection), amount, MAX_SPOTS_PER_PLAYER)


def _resolve(bet: dict, dice: tuple[int, int], point_before: int | None):
    """Outcome of one roll for one bet.

    Returns None (no action), ("win", winnings, stays_up), ("lose",), ("push",),
    ("return",) for odds that were off, or ("move", number) for a come bet travelling.
    """
    total = sum(dice)
    hard = dice[0] == dice[1]
    kind, amount = bet["kind"], bet["amount"]
    number = int(bet["selection"]) if bet["selection"].isdigit() else None
    come_out = point_before is None

    def odds_win(n: int, lay: bool) -> int:
        numerator, denominator = TRUE_ODDS[n]
        return amount * denominator // numerator if lay else amount * numerator // denominator

    if kind == "pass" or (kind == "come" and number is None):
        target = point_before if kind == "pass" else None
        if target is None:
            if total in (7, 11):
                return ("win", amount, False)
            if total in (2, 3, 12):
                return ("lose",)
            return None if kind == "pass" else ("move", total)
        return ("win", amount, False) if total == target else ("lose",) if total == 7 else None
    if kind == "dontpass" or (kind == "dontcome" and number is None):
        target = point_before if kind == "dontpass" else None
        if target is None:
            if total in (2, 3):
                return ("win", amount, False)
            if total == 12:
                return ("push",)
            if total in (7, 11):
                return ("lose",)
            return None if kind == "dontpass" else ("move", total)
        return ("win", amount, False) if total == 7 else ("lose",) if total == target else None
    if kind == "come":
        return ("win", amount, False) if total == number else ("lose",) if total == 7 else None
    if kind == "dontcome":
        return ("win", amount, False) if total == 7 else ("lose",) if total == number else None
    if kind == "pass_odds":
        return ("win", odds_win(point_before, False), False) if total == point_before else ("lose",) if total == 7 else None
    if kind == "dontpass_odds":
        return ("win", odds_win(point_before, True), False) if total == 7 else ("lose",) if total == point_before else None
    if kind == "come_odds":  # off on the come-out roll: returned when the come bet settles
        if total not in (number, 7):
            return None
        if come_out:
            return ("return",)
        return ("win", odds_win(number, False), False) if total == number else ("lose",)
    if kind == "dontcome_odds":
        return ("win", odds_win(number, True), False) if total == 7 else ("lose",) if total == number else None
    if kind == "place":
        if come_out:
            return None
        if total == number:
            numerator, denominator = PLACE_PAYS[number]
            return ("win", amount * numerator // denominator, True)
        return ("lose",) if total == 7 else None
    if kind == "hard":
        if come_out:
            return None
        if total == number:
            return ("win", amount * HARDWAY_PAYS[number], True) if hard else ("lose",)
        return ("lose",) if total == 7 else None
    if kind == "big":
        return ("win", amount, True) if total == number else ("lose",) if total == 7 else None
    if kind == "field":
        return ("win", amount * FIELD_PAYS[total], False) if total in FIELD_PAYS else ("lose",)
    if kind in PROPOSITIONS:
        totals, pays, _ = PROPOSITIONS[kind]
        return ("win", amount * pays, False) if total in totals else ("lose",)
    return None


def _settle_bets(bets: list[dict], dice: tuple[int, int], point_before: int | None):
    """Apply one roll to a seat's bets: (bets left on the table, results, payout, stake lost)."""
    remaining: list[dict] = []
    results: list[dict] = []
    payout = 0
    for bet in bets:
        outcome = _resolve(bet, dice, point_before)
        if outcome is None:
            remaining.append(bet)
            continue
        result = {"kind": bet["kind"], "selection": bet["selection"], "description": bet["description"],
                  "amount": bet["amount"], "payout": 0, "won": False}
        if outcome[0] == "move":
            moved_to = str(outcome[1])
            remaining.append({**bet, "selection": moved_to, "description": _describe(bet["kind"], moved_to)})
            result["outcome"] = f"moved to {moved_to}"
        elif outcome[0] == "win":
            winnings, stays_up = outcome[1], outcome[2]
            result.update(won=True, payout=winnings, outcome=f"won {winnings:,}")
            payout += winnings if stays_up else winnings + bet["amount"]
            if stays_up:
                remaining.append(bet)
                result["outcome"] += " · stays up"
        elif outcome[0] in ("push", "return"):
            payout += bet["amount"]
            result["outcome"] = "push" if outcome[0] == "push" else "returned (odds off)"
        else:
            result["outcome"] = "lost"
        results.append(result)
    stake_left = sum(bet["amount"] for bet in bets) - sum(bet["amount"] for bet in remaining)
    # A come bet that travels to a number joins any come bet already there.
    merged: dict[tuple[str, str], dict] = {}
    for bet in remaining:
        key = (bet["kind"], bet["selection"])
        if key in merged:
            merged[key]["amount"] += bet["amount"]
        else:
            merged[key] = bet
    return list(merged.values()), results, payout, stake_left


# --- Client state ----------------------------------------------------------------------

def _public_state(for_sid: str) -> dict:
    own_player = players.get(for_sid, {})
    roll = None
    if phase == "rolling" and current_roll:
        elapsed = time.monotonic() - current_roll["started_at"]
        roll = {
            "id": current_roll["id"],
            "dice": list(current_roll["dice"]),
            "total": sum(current_roll["dice"]),
            "duration_ms": int(ROLL_SECONDS * 1000),
            "remaining_ms": max(0, int((ROLL_SECONDS - elapsed) * 1000)),
        }
    shooter = players.get(shooter_sid)
    return {
        "phase": phase,
        "own_sid": for_sid,
        "own_is_admin": bool(own_player.get("is_admin")),
        "round_number": roll_number,
        "message": message,
        "point": point,
        "shooter": shooter["name"] if shooter else None,
        "shooter_sid": shooter_sid,
        "ready_to_roll": _ready_to_roll(),
        "last_roll": dict(last_roll) if last_roll else None,
        "roll": roll,
        "history": [dict(entry) for entry in history],
        "forced_next_roll": list(forced_next_roll) if forced_next_roll and own_player.get("is_admin") else None,
        "players": [
            {
                **public_seat(sid, player, accounts),
                "bets": chips.public_bets(player),
                "last_bets": [dict(bet) for bet in player["last_bets"]],
                "on_table": chips.table_total(player),
                "can_rebet": bool(player["previous_bets"]),
                "can_undo": bool(player["bet_actions"]),
                "ready_eligible": _ready_eligible(player),
            }
            for sid, player in players.items()
        ],
        "min_bet": MIN_BET,
        "max_bet": MAX_BET,
        "max_bets_per_player": MAX_SPOTS_PER_PLAYER,
    }


def _broadcast() -> None:
    for sid, player in list(players.items()):
        if player["connected"]:
            socketio.emit("craps_state", _public_state(sid), to=sid)


def _ready_eligible(player: dict) -> bool:
    """Seats the roll waits on: anyone with chips down, or credits to place some."""
    return player["connected"] and (bool(player["bets"]) or player["credits"] >= MIN_BET)


def _player_for_betting(sid: str) -> dict | None:
    player = players.get(sid)
    if not player:
        _error("Join the craps table first.")
        return None
    if phase == "rolling":
        _error("No more bets — the dice are rolling.")
        return None
    return player


def _bets_changed(player: dict) -> None:
    player["ready"] = False
    player["status"] = "bets placed" if player["bets"] else "no bets"


# --- Socket.IO events ------------------------------------------------------------------------

def on_refresh_state(_data=None):
    with lock:
        sid = request_sid()
        if sid in players and players[sid]["connected"]:
            emit("craps_state", _public_state(sid), to=sid)


def on_set_bet(data):
    with lock:
        player = players.get(request_sid())
        if not player:
            _error("Join the craps table first.")
            return
        amount = parse_bet(data, "chip value")
        if amount is not None and save_bet_preference(app, accounts, player, amount):
            _broadcast()


def on_add_bet(data):
    global message
    with lock:
        player = _player_for_betting(request_sid())
        if not player:
            return
        kind = str((data or {}).get("kind", "")).strip().lower()
        selection = str((data or {}).get("selection", "")).strip()
        error = _place(player, kind, selection, player["bet"])
        if error:
            _error(error)
            return
        action = player["bet_actions"][-1]
        player["last_bets"] = []
        _bets_changed(player)
        message = f"{player['name']} bet {action['amount']:,} on {_describe(action['kind'], action['selection'])}."
        _broadcast()


def on_remove_bet(data):
    """Remove new chips from a spot, or take down a bet that is allowed to come down."""
    global message
    with lock:
        player = _player_for_betting(request_sid())
        if not player:
            return
        try:
            index = int((data or {}).get("index"))
        except (TypeError, ValueError):
            return
        if not 0 <= index < len(player["bets"]):
            return
        bet = player["bets"][index]
        if bet["staged"]:
            removed = chips.remove_staged(player, index)
            message = f"{player['name']} removed {removed['amount']:,} from {removed['description']}."
        else:
            if not _removable(bet):
                _error(f"{bet['description']} is a contract bet and stays up until it wins or loses.")
                return
            # Taking down a don't bet also takes down the odds laid on it.
            taken = [bet] + [
                odds for odds in player["bets"]
                if (bet["kind"], odds["kind"]) in {("dontpass", "dontpass_odds"), ("dontcome", "dontcome_odds")}
                and odds["selection"] == bet["selection"]
            ]
            refund = sum(item["amount"] - item["staged"] for item in taken)
            try:
                player["credits"] = accounts.return_round_wager(TABLE_ROUND_ID, player["account_id"], refund)
            except Exception:
                app.logger.exception("Could not take down a craps bet")
                _error("Could not take that bet down because account storage failed.")
                return
            for item in taken:
                player["bets"].remove(item)
            message = f"{player['name']} took down {bet['description']} ({refund:,})."
        _bets_changed(player)
        _broadcast()


def on_undo_bet(_data=None):
    global message
    with lock:
        player = _player_for_betting(request_sid())
        undone = chips.undo_chips(player) if player else None
        if not undone:
            return
        message = f"{player['name']} took back {undone['amount']:,} from {undone['description']}."
        _bets_changed(player)
        _broadcast()


def on_clear_bets(_data=None):
    global message
    with lock:
        player = _player_for_betting(request_sid())
        if not player:
            return
        cleared = chips.clear_staged(player)
        if cleared:
            message = f"{player['name']} cleared {cleared:,} in new bets."
        _bets_changed(player)
        _broadcast()


def on_rebet(_data=None):
    global message
    with lock:
        player = _player_for_betting(request_sid())
        if not player:
            return
        error = chips.rebet(player, lambda kind, selection, amount: _place(player, kind, selection, amount))
        if error:
            _error(error)
            _broadcast()
            return
        player["last_bets"] = []
        _bets_changed(player)
        message = f"{player['name']} repeated their last bets ({chips.staged_total(player):,} credits)."
        _broadcast()


def on_toggle_ready(_data=None):
    global message
    with lock:
        player = players.get(request_sid())
        if not player or not player["connected"]:
            _error("Join the craps table first.")
            return
        if phase == "rolling":
            _error("The dice are already rolling.")
            return
        player["ready"] = not player["ready"]
        if player["ready"]:
            message = f"{player['name']} is ready." if player["bets"] else f"{player['name']} is sitting out this roll."
        else:
            message = f"{player['name']} is no longer ready."
        _broadcast()
        _roll_if_everyone_ready()


# --- Rolling --------------------------------------------------------------------------------

def _ready_to_roll() -> bool:
    """Every eligible seat is ready and somebody has chips down: the shooter may roll."""
    if phase == "rolling" or shooter_sid not in players:
        return False
    eligible = everyone_ready(players, _ready_eligible)
    return bool(eligible) and any(player["bets"] for player in eligible)


def _roll_if_everyone_ready() -> None:
    """Once everyone is ready, hand the dice to the shooter (who rolls with craps_roll)."""
    global message
    if phase == "rolling":
        return

    def call_for_roll() -> None:
        global message
        message = f"Everyone is ready. {players[shooter_sid]['name']}, roll the dice!"

    notice = chips.play_if_everyone_ready(
        players, _ready_eligible, lambda player: bool(player["bets"]), call_for_roll,
        "Everyone sat out, so there is nothing to roll for. Place bets and ready up.",
    )
    if notice:
        message = notice
    _broadcast()


def on_roll(_data=None):
    """The shooter rolls once every player is ready."""
    global message
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player:
            _error("Join the craps table first.")
            return
        if sid != shooter_sid:
            shooter = players.get(shooter_sid)
            _error(f"Only the shooter ({shooter['name']}) can roll the dice." if shooter else "Only the shooter can roll.")
            return
        if not _ready_to_roll():
            _error("Wait until every player is ready before rolling.")
            return
        error = _begin_roll(f"{player['name']} rolls the dice!")
        if error:
            for seat in players.values():
                seat["ready"] = False
            message = error
            _error(error)
            _broadcast()


def _begin_roll(start_message: str) -> str | None:
    """Debit new chips, settle the roll and start the dice. Returns an error, if any."""
    global phase, roll_number, message, current_roll, pending_reveal, forced_next_roll
    seats = [(sid, player) for sid, player in players.items() if player["bets"]]
    if not seats:
        return "Nobody has a bet on the table, so the dice can't roll."
    wagers = [(player["account_id"], chips.staged_total(player)) for _, player in seats if chips.staged_total(player)]
    try:
        balances = accounts.debit_round_bets(TABLE_ROUND_ID, wagers) if wagers else {}
    except ValueError:
        return "A player no longer has enough credits for their new bets."
    except Exception:
        app.logger.exception("Could not debit craps bets")
        return "Could not roll because account storage failed."

    if forced_next_roll is not None:
        dice, forced_next_roll = forced_next_roll, None
    else:
        dice = (random.randint(1, 6), random.randint(1, 6))
    phase = "rolling"
    roll_number += 1
    reveals: dict[str, dict] = {}
    payouts: dict[int, int] = {}
    carried: dict[int, int] = {}
    for player in players.values():
        player["ready"] = False
        if not player["bets"]:  # Sitting this roll out: clear the previous roll's results.
            player["last_bets"] = []
            player["round_credit_change"] = None
            player["status"] = "sitting out"
    for sid, player in seats:
        if player["account_id"] in balances:
            player["credits"] = balances[player["account_id"]]
        chips.lock_staged(player)
        player["status"] = "rolling"
        remaining, results, payout, stake_left = _settle_bets(
            [dict(bet) for bet in player["bets"]], dice, point,
        )
        reveals[sid] = {
            "bets": remaining,
            "last_bets": results,
            "round_credit_change": payout - stake_left if results else None,
        }
        payouts[player["account_id"]] = payouts.get(player["account_id"], 0) + payout
        carried[player["account_id"]] = carried.get(player["account_id"], 0) + chips.table_total({"bets": remaining})

    failed = False
    settled: dict[int, int] = {}
    try:
        settled = accounts.settle_round_payouts(TABLE_ROUND_ID, list(payouts.items()), list(carried.items()))
    except Exception:
        app.logger.exception("Craps settlement failed")
        failed = True

    current_roll = {"id": roll_number, "dice": dice, "started_at": time.monotonic(), "point_before": point}
    pending_reveal = {"players": reveals, "balances": settled, "failed": failed}
    message = start_message
    _broadcast()
    expected = roll_number
    chips.reveal_later(socketio, lock, ROLL_SECONDS, lambda: _finish_roll(expected))
    return None


def _roll_summary(dice: tuple[int, int], point_before: int | None) -> str:
    total = sum(dice)
    shooter = players.get(shooter_sid)
    rolled = f"{shooter['name'] if shooter else 'The shooter'} rolled {dice[0]}-{dice[1]} = {total}"
    if point_before is None:
        if total in (7, 11):
            return f"{rolled}. Natural winner on the come-out!"
        if total in (2, 3, 12):
            return f"{rolled}. Craps." + (" Bar 12 — don't pass pushes." if total == 12 else "")
        return f"{rolled}. The point is {total}."
    if total == point_before:
        return f"{rolled}. Point made — pass line wins! New come-out roll."
    if total == 7:
        return f"{rolled}. Seven out. The dice pass to the next shooter."
    return f"{rolled}. The point is still {point_before}."


def _finish_roll(expected_roll: int) -> None:
    global phase, message, last_roll, current_roll, pending_reveal, point
    if phase != "rolling" or not current_roll or current_roll["id"] != expected_roll:
        return
    dice, point_before = current_roll["dice"], current_roll["point_before"]
    total = sum(dice)
    reveal = pending_reveal or {"players": {}, "balances": {}, "failed": False}
    for sid, outcome in reveal["players"].items():
        player = players.get(sid)
        if not player:
            continue
        player["bets"] = outcome["bets"]
        player["last_bets"] = outcome["last_bets"]
        player["round_credit_change"] = outcome["round_credit_change"]
        change = outcome["round_credit_change"]
        player["status"] = (
            "no action" if change is None else f"won {change:,}" if change > 0
            else "lost" if change < 0 else "even"
        )
    for player in players.values():
        if player["account_id"] in reveal["balances"]:
            player["credits"] = reveal["balances"][player["account_id"]]
    summary = _roll_summary(dice, point_before)
    if point_before is None and total in POINTS:
        point = total
    elif point_before is not None and total in (7, point_before):
        point = None
        if total == 7:
            _next_shooter()
    last_roll = {"dice": list(dice), "total": total, "roll_number": current_roll["id"], "hard": dice[0] == dice[1]}
    history.insert(0, {"dice": list(dice), "total": total})
    del history[HISTORY_LENGTH:]
    phase = "betting"
    current_roll = None
    pending_reveal = None
    message = summary if not reveal["failed"] else (
        f"{summary} Credits could not be settled; wagers will be refunded at server restart."
    )
    _broadcast()

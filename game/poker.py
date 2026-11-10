"""Server-authoritative no-limit Texas Hold'em table and Socket.IO actions.

Chips are the players' shared account credits: every blind, call and raise is debited
as it happens (and recorded for a refund if the server stops mid-hand), and the pots
are paid out at the end of the hand.
"""

from __future__ import annotations

import uuid
from collections import Counter, deque
from itertools import combinations

from flask_socketio import emit

from game.common import (
    MAX_BET, MIN_BET, DecisionTimer, ManualDraw, card_rank, card_suit, error as _error, everyone_ready, new_seat,
    parse_decision_seconds, public_seat, register_handlers, run_decision_timer, seat_refusal,
    set_credit_balance, set_profile_image_version, shuffled_cards, stack_card,
)


ROUND_PHASES = ("preflop", "flop", "turn", "river")
STREET_NAMES = {"preflop": "Pre-flop", "flop": "Flop", "turn": "Turn", "river": "River"}
DEFAULT_BIG_BLIND = 10
# Newcomers can sit down during a hand and are dealt in from the next one.
JOINS_MID_ROUND = True
HIDDEN_CARD = "🂠"

players: dict[str, dict] = {}
# The next hand's deck is shuffled ahead of time so an admin can stack it between hands.
deck: deque[str] = shuffled_cards()
board: list[str] = []
phase = "waiting"  # waiting, preflop, flop, turn, river, round_over
hand_sids: list[str] = []  # seats dealt into the current hand, in seat order
dealer_sid: str | None = None
small_blind_sid: str | None = None
big_blind_sid: str | None = None
turn_sid: str | None = None
current_bet = 0
min_raise = DEFAULT_BIG_BLIND
big_blind = DEFAULT_BIG_BLIND
button_index = -1
message = "Sign in to take a seat at the poker table."
round_number = 0
active_round_id: str | None = None
showdown_pots: list[dict] = []
timer = DecisionTimer()

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
        "poker_refresh_state": on_refresh_state,
        "poker_toggle_ready": on_toggle_ready,
        "poker_action": on_action,
    })


def small_blind() -> int:
    return max(MIN_BET, big_blind // 2)


def is_round_in_progress() -> bool:
    return phase in ROUND_PHASES


# --- Account adapter hooks -------------------------------------------------------

def attach_player(sid: str, account: dict) -> bool:
    global message
    with lock:
        mid_hand = is_round_in_progress()
        if not mid_hand:  # Seats still in the current hand stay until it is settled.
            _drop_disconnected_seats()
        refusal = seat_refusal(players, sid, account)
        if refusal:
            _error(refusal)
            return False
        players[sid] = new_seat(account, **_empty_hand_fields(), status="next hand" if mid_hand else "waiting")
        message = f"{account['username']} sat down" + (" and will be dealt in next hand." if mid_hand else ".")
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
            if not _ready_eligible(player):
                player["ready"] = False
        if changed:
            _broadcast()
            _start_if_everyone_ready()


def disconnect_player(sid: str) -> None:
    global message
    with lock:
        player = players.get(sid)
        if not player:
            return
        player["connected"] = False
        player["ready"] = False
        in_live_hand = is_round_in_progress() and sid in hand_sids
        if in_live_hand and not player["folded"]:
            player["folded"] = True
            player["status"] = "left"
            message = f"{player['name']} left the table and folded."
            if sid == turn_sid:
                _advance()
            elif len(_live_sids()) == 1:
                _finish_hand()
        elif not in_live_hand:
            players.pop(sid, None)
            message = f"{player['name']} left the table."
            _start_if_everyone_ready()
        if player["is_admin"] and draws.disable():
            message = "Manual draw mode disabled because the admin disconnected."
        _broadcast()


def handle_admin_command(data: dict) -> None:
    global message, big_blind, min_raise
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player:
            _error("Sign in with an admin account first.")
            return
        parts = str((data or {}).get("command", "")).strip().split()
        command = parts[0].lower() if parts else ""
        if command in {"/forcestart", "/force-start"}:
            if len(parts) > 2:
                _error("Usage: /forcestart [decision-seconds]")
                return
            seconds = parse_decision_seconds(parts[1] if len(parts) == 2 else timer.seconds)
            if seconds is None:
                return
            if is_round_in_progress():
                _error("A hand is already in progress.")
                return
            refusal = _begin_hand(seconds, f"Admin {player['name']} dealt a new hand.")
            if refusal:
                _error(refusal)
            return
        if command == "/timer":
            blocked = None if turn_sid and is_round_in_progress() else "There is no player deciding right now."
            ok, notice = timer.admin_command(parts, blocked)
            if not ok:
                _error(notice)
                return
            _notify(sid, notice)
            _broadcast()
            return
        if command == "/skip":
            if len(parts) != 1:
                _error("Usage: /skip")
                return
            if not turn_sid or not is_round_in_progress():
                _error("There is no player deciding right now.")
                return
            _check_or_fold(turn_sid, "was skipped by an admin")
            _broadcast()
            return
        draw_result = draws.admin_command(command, parts)
        if draw_result is not None:
            ok, notice = draw_result
            if not ok:
                _error(notice)
                return
            _notify(sid, notice)
            _broadcast()
            return
        if command == "/blinds":
            if len(parts) != 2 or not parts[1].isdigit():
                _error("Usage: /blinds big-blind (the small blind is half)")
                return
            amount = int(parts[1])
            if not 2 <= amount <= MAX_BET:
                _error(f"The big blind must be between 2 and {MAX_BET:,} credits.")
                return
            if is_round_in_progress():
                _error("Change the blinds between hands.")
                return
            big_blind = amount
            min_raise = amount
            for seat in players.values():
                if not _ready_eligible(seat):
                    seat["ready"] = False
            message = f"Blinds are now {small_blind():,}/{big_blind:,}."
            _notify(sid, message)
            _broadcast()
            return
        if command == "/stackcard":
            if len(parts) != 2:
                _error("Usage: /stackcard rank[suit], for example /stackcard A or /stackcard 10H")
                return
            card = stack_card(deck, parts[1])
            if card is None:
                _error("That card is not in the remaining deck.")
                return
            _notify(sid, f"Moved {card} to the top of the deck; it is the next card dealt.")
            _broadcast()
            return
        _error(
            "Poker commands: /forcestart [seconds] · /skip · /timer seconds|pause|resume · "
            "/manualdraw on|off · /draw · /blinds amount · /stackcard rank[suit] · /setcredits username amount"
        )


# --- Hand evaluation ----------------------------------------------------------------

RANK_VALUES = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10,
               "J": 11, "Q": 12, "K": 13, "A": 14}
VALUE_NAMES = {2: "Twos", 3: "Threes", 4: "Fours", 5: "Fives", 6: "Sixes", 7: "Sevens", 8: "Eights",
               9: "Nines", 10: "Tens", 11: "Jacks", 12: "Queens", 13: "Kings", 14: "Aces"}
HIGH_NAMES = {value: name[:-1] if not name.endswith("xes") else name[:-2] for value, name in VALUE_NAMES.items()}


def _score_five(cards: tuple[str, ...]) -> tuple:
    """Comparable score for a five-card hand: (category, tie-breakers...)."""
    values = sorted((RANK_VALUES[card_rank(card)] for card in cards), reverse=True)
    flush = len({card_suit(card) for card in cards}) == 1
    unique = sorted(set(values), reverse=True)
    straight_high = None
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            straight_high = unique[0]
        elif unique == [14, 5, 4, 3, 2]:
            straight_high = 5
    groups = sorted(Counter(values).items(), key=lambda item: (item[1], item[0]), reverse=True)
    counts = [count for _, count in groups]
    ordered = [value for value, _ in groups]
    if straight_high and flush:
        return (8, straight_high)
    if counts[0] == 4:
        return (7, *ordered)
    if counts[:2] == [3, 2]:
        return (6, *ordered)
    if flush:
        return (5, *values)
    if straight_high:
        return (4, straight_high)
    if counts[0] == 3:
        return (3, *ordered)
    if counts[:2] == [2, 2]:
        return (2, *ordered)
    if counts[0] == 2:
        return (1, *ordered)
    return (0, *values)


def best_hand(cards: list[str]) -> tuple[tuple, list[str]]:
    """The best five-card score and cards out of up to seven."""
    return max(((_score_five(combo), list(combo)) for combo in combinations(cards, 5)), key=lambda item: item[0])


def describe_score(score: tuple) -> str:
    category, *values = score
    if category == 8:
        return "Royal flush" if values[0] == 14 else f"Straight flush, {HIGH_NAMES[values[0]]} high"
    if category == 7:
        return f"Four of a kind, {VALUE_NAMES[values[0]]}"
    if category == 6:
        return f"Full house, {VALUE_NAMES[values[0]]} over {VALUE_NAMES[values[1]]}"
    if category == 5:
        return f"Flush, {HIGH_NAMES[values[0]]} high"
    if category == 4:
        return f"Straight, {HIGH_NAMES[values[0]]} high"
    if category == 3:
        return f"Three of a kind, {VALUE_NAMES[values[0]]}"
    if category == 2:
        return f"Two pair, {VALUE_NAMES[values[0]]} and {VALUE_NAMES[values[1]]}"
    if category == 1:
        return f"Pair of {VALUE_NAMES[values[0]]}"
    return f"{HIGH_NAMES[values[0]]} high"


# --- Table helpers ---------------------------------------------------------------------

def _empty_hand_fields() -> dict:
    return {
        "hole": [], "folded": False, "all_in": False, "acted": False,
        "street_bet": 0, "total_bet": 0, "hand_label": "", "best_cards": [], "showed": False,
        "won": 0, "last_action": "",
    }


def _notify(sid: str, text: str) -> None:
    socketio.emit("notice", {"text": text}, to=sid)


def _drop_disconnected_seats() -> None:
    for sid in [sid for sid, player in players.items() if not player["connected"]]:
        players.pop(sid, None)


def _ready_eligible(player: dict) -> bool:
    return player["connected"] and player["credits"] >= big_blind


def _live_sids() -> list[str]:
    return [sid for sid in hand_sids if not players[sid]["folded"]]


def _next_in_hand(after_sid: str | None, predicate) -> str | None:
    """The next seat in the hand after `after_sid` (wrapping) that satisfies predicate."""
    if not hand_sids:
        return None
    start = hand_sids.index(after_sid) + 1 if after_sid in hand_sids else 0
    for offset in range(len(hand_sids)):
        sid = hand_sids[(start + offset) % len(hand_sids)]
        if predicate(players[sid]):
            return sid
    return None


def _needs_to_act(player: dict) -> bool:
    return (
        not player["folded"] and not player["all_in"]
        and (not player["acted"] or player["street_bet"] < current_bet)
    )


def _pot_total() -> int:
    return sum(players[sid]["total_bet"] for sid in hand_sids if sid in players)


def _commit(sid: str, amount: int) -> bool:
    """Move `amount` of the player's credits into the pot. Reports failures to the client."""
    player = players[sid]
    if amount <= 0:
        return True
    try:
        player["credits"] = accounts.add_round_wager(active_round_id, player["account_id"], amount)
    except ValueError:
        _error("You don't have enough credits for that.")
        return False
    except Exception:
        app.logger.exception("Could not record a poker wager")
        _error("Could not place that bet because account storage failed.")
        return False
    player["street_bet"] += amount
    player["total_bet"] += amount
    if player["credits"] <= 0:
        player["all_in"] = True
    return True


def _run_draw_step(step: dict, card: str | None) -> None:
    """Run one queued dealing step (see ManualDraw)."""
    global message
    kind = step["kind"]
    if kind == "hole":
        players[step["sid"]]["hole"].append(card)
    elif kind == "board":
        board.append(card)
    elif kind == "open_betting":
        _set_turn(_next_in_hand(big_blind_sid, _needs_to_act))
    elif kind == "open_street":
        message = f"{STREET_NAMES[phase]}: {' '.join(board)}"
        _set_turn(_next_in_hand(dealer_sid, _needs_to_act))
    elif kind == "showdown":
        _finish_hand()


def _draw_target(step: dict) -> str:
    if step["kind"] == "hole":
        player = players.get(step["sid"])
        return f"Hole card {step['card_number']} to {player['name'] if player else 'player'}"
    if step["kind"] == "board":
        return f"Board card {len(board) + 1} ({STREET_NAMES[step['street']].lower()})"
    return step["kind"].replace("_", " ")


draws = ManualDraw(lambda: deck, _run_draw_step, _draw_target)


# --- Hand flow ---------------------------------------------------------------------------

def _start_if_everyone_ready() -> None:
    global message
    if is_round_in_progress():
        return
    eligible = everyone_ready(players, _ready_eligible)
    if not eligible:
        return
    if len(eligible) < 2:
        message = "Waiting for at least one more player to ready up."
        return
    refusal = _begin_hand(timer.seconds, "Everyone is ready. Shuffle up and deal!")
    if refusal:
        for player in players.values():
            player["ready"] = False
        message = refusal
        _broadcast()


def _begin_hand(decision_seconds: int, start_message: str) -> str | None:
    """Post blinds, deal hole cards and start pre-flop betting. Returns a refusal, if any."""
    global deck, board, phase, hand_sids, dealer_sid, small_blind_sid, big_blind_sid, turn_sid
    global current_bet, min_raise, button_index, message, round_number, active_round_id, showdown_pots
    _drop_disconnected_seats()
    seated = [sid for sid, player in players.items() if _ready_eligible(player)]
    if len(seated) < 2:
        return f"At least two players with {big_blind:,} credits (the big blind) are needed to deal."

    button_index = (button_index + 1) % len(seated)
    dealer = seated[button_index]
    if len(seated) == 2:
        # Heads-up: the button posts the small blind and acts first before the flop.
        sb, bb = dealer, seated[(button_index + 1) % 2]
    else:
        sb = seated[(button_index + 1) % len(seated)]
        bb = seated[(button_index + 2) % len(seated)]
    blinds = {sb: min(small_blind(), players[sb]["credits"]), bb: min(big_blind, players[bb]["credits"])}
    round_id = uuid.uuid4().hex
    try:
        balances = accounts.debit_round_bets(
            round_id, [(players[sid]["account_id"], amount) for sid, amount in blinds.items()],
        )
    except ValueError:
        return "A blind could not be posted because of insufficient credits."
    except Exception:
        app.logger.exception("Could not post poker blinds")
        return "Could not deal because account storage failed."

    active_round_id = round_id
    round_number += 1
    timer.seconds = decision_seconds
    if len(deck) != 52:
        deck = shuffled_cards()
    board = []
    showdown_pots = []
    hand_sids = seated
    dealer_sid, small_blind_sid, big_blind_sid = dealer, sb, bb
    for sid, player in players.items():
        player.update(_empty_hand_fields())
        player["ready"] = False
        player["round_credit_change"] = None
        player["status"] = "in hand" if sid in hand_sids else "sitting out"
    for sid, amount in blinds.items():
        player = players[sid]
        player["credits"] = balances[player["account_id"]]
        player["street_bet"] = player["total_bet"] = amount
        player["all_in"] = player["credits"] <= 0
        player["last_action"] = "Small blind" if sid == sb else "Big blind"
    current_bet = big_blind
    min_raise = big_blind
    phase = "preflop"
    turn_sid = None
    message = start_message
    # Deal one card at a time, starting left of the button, then open the betting.
    deal_order = seated[button_index + 1:] + seated[:button_index + 1]
    draws.clear()
    for card_number in (1, 2):
        draws.add(*({"kind": "hole", "sid": sid, "card_number": card_number} for sid in deal_order))
    draws.add({"kind": "open_betting", "draws": False})
    draws.pump()
    _broadcast()
    return None


def _set_turn(sid: str | None) -> None:
    global turn_sid
    turn_sid = sid
    timer.reset(sid is not None)
    if sid is None:
        _end_street()


def _advance() -> None:
    """After an action: pass the turn, close the street, or end the hand."""
    if len(_live_sids()) == 1:
        _finish_hand()
        return
    _set_turn(_next_in_hand(turn_sid, _needs_to_act))


def _end_street() -> None:
    global phase, current_bet, min_raise, message
    if len(_live_sids()) == 1:
        _finish_hand()
        return
    for sid in hand_sids:
        players[sid]["street_bet"] = 0
        players[sid]["acted"] = False
    current_bet = 0
    min_raise = big_blind
    can_still_bet = [sid for sid in _live_sids() if not players[sid]["all_in"]]
    if len(can_still_bet) < 2:
        # Nobody is left to bet against: run out the rest of the board.
        message = "Everyone is all-in. Running out the board."
        streets = ["flop"] * 3 + ["turn", "river"]
        draws.add(*({"kind": "board", "street": street} for street in streets[len(board):]))
        draws.add({"kind": "showdown", "draws": False})
        draws.pump()
        return
    if phase == "river":
        _finish_hand()
        return
    phase, cards = {"preflop": ("flop", 3), "flop": ("turn", 1), "turn": ("river", 1)}[phase]
    draws.add(*({"kind": "board", "street": phase} for _ in range(cards)))
    draws.add({"kind": "open_street", "draws": False})
    draws.pump()


def _build_pots() -> list[dict]:
    """Split the hand's contributions into the main pot and side pots."""
    contributions = {sid: players[sid]["total_bet"] for sid in hand_sids}
    live = set(_live_sids())
    pots: list[dict] = []
    previous = 0
    for level in sorted({amount for amount in contributions.values() if amount > 0}):
        amount = sum(min(paid, level) - min(paid, previous) for paid in contributions.values())
        eligible = [sid for sid in hand_sids if sid in live and contributions[sid] >= level]
        previous = level
        if not eligible and pots:
            pots[-1]["amount"] += amount
        elif pots and pots[-1]["eligible"] == eligible:
            pots[-1]["amount"] += amount
        else:
            pots.append({"amount": amount, "eligible": eligible})
    for pot in pots:
        if not pot["eligible"]:  # Only possible if every contributor folded; give it to whoever is left.
            pot["eligible"] = list(live)
    return pots


def _finish_hand() -> None:
    global phase, turn_sid, active_round_id, message, showdown_pots, deck
    live = _live_sids()
    showdown = len(live) > 1
    scores: dict[str, tuple] = {}
    if showdown:
        for sid in live:
            player = players[sid]
            score, cards = best_hand(player["hole"] + board)
            scores[sid] = score
            player["hand_label"] = describe_score(score)
            player["best_cards"] = cards
            player["showed"] = True

    winnings: dict[str, int] = {sid: 0 for sid in hand_sids}
    # Odd chips go to the first winners clockwise from the button.
    first = hand_sids.index(_next_in_hand(dealer_sid, lambda _player: True))
    clockwise = hand_sids[first:] + hand_sids[:first]
    showdown_pots = []
    for index, pot in enumerate(_build_pots()):
        if showdown:
            best = max(scores[sid] for sid in pot["eligible"])
            winners = [sid for sid in pot["eligible"] if scores[sid] == best]
        else:
            winners = pot["eligible"]
        share, remainder = divmod(pot["amount"], len(winners))
        for sid in winners:
            winnings[sid] += share
        for sid in [sid for sid in clockwise if sid in winners][:remainder]:
            winnings[sid] += 1
        showdown_pots.append({
            "name": "Main pot" if index == 0 else f"Side pot {index}",
            "amount": pot["amount"],
            "winners": [players[sid]["name"] for sid in winners],
            "hand": players[winners[0]]["hand_label"] if showdown else "",
        })

    payouts: dict[int, int] = {}
    for sid, amount in winnings.items():
        if amount:
            account_id = players[sid]["account_id"]
            payouts[account_id] = payouts.get(account_id, 0) + amount
    failed = False
    balances: dict[int, int] = {}
    try:
        balances = accounts.settle_round_payouts(active_round_id, list(payouts.items()))
    except Exception:
        app.logger.exception("Poker pot settlement failed")
        failed = True

    for sid in hand_sids:
        player = players[sid]
        player["won"] = winnings[sid]
        player["round_credit_change"] = winnings[sid] - player["total_bet"]
        if player["account_id"] in balances:
            player["credits"] = balances[player["account_id"]]
        if winnings[sid] > 0:
            player["status"] = "won"
        elif player["folded"]:
            player["status"] = "left" if not player["connected"] else "folded"
        else:
            player["status"] = "lost"

    pot_text = " · ".join(
        f"{pot['name']} {pot['amount']:,} to {' & '.join(pot['winners'])}"
        + (f" ({pot['hand']})" if pot["hand"] else "")
        for pot in showdown_pots
    )
    if failed:
        message = "The hand ended, but credits could not be paid. Wagers will be refunded at server restart."
    elif showdown:
        message = f"Showdown — {pot_text}."
    else:
        message = f"{showdown_pots[0]['winners'][0]} wins {_pot_total():,} uncontested."
    phase = "round_over"
    turn_sid = None
    active_round_id = None
    timer.reset(False)
    draws.clear()
    deck = shuffled_cards()


def _check_or_fold(sid: str, reason: str = "timed out") -> None:
    """Timeout and admin-skip default: check when free, otherwise fold."""
    global message
    player = players[sid]
    if player["street_bet"] >= current_bet:
        player["acted"] = True
        player["last_action"] = "Check"
        message = f"{player['name']} {reason} and checks."
    else:
        player["folded"] = True
        player["status"] = "folded"
        player["last_action"] = "Fold"
        message = f"{player['name']} {reason} and folds."
    _notify(sid, message)
    _advance()


def decision_timer_loop() -> None:
    """Background task (started by the server) that runs the decision timer."""
    run_decision_timer(
        socketio, lock, timer,
        is_waiting=lambda: is_round_in_progress() and turn_sid is not None,
        on_expire=lambda: _check_or_fold(turn_sid),
        broadcast=_broadcast,
    )


# --- Client state ----------------------------------------------------------------------------

def _visible_hole(sid: str, for_sid: str) -> list[str]:
    player = players[sid]
    if not player["hole"]:
        return []
    if sid == for_sid or player["showed"]:
        return player["hole"][:]
    return [HIDDEN_CARD] * len(player["hole"])


def _own_options(for_sid: str) -> dict:
    player = players.get(for_sid)
    if not player or for_sid != turn_sid or not is_round_in_progress():
        return {"can_act": False}
    to_call = max(0, current_bet - player["street_bet"])
    stack = player["credits"]
    max_to = player["street_bet"] + stack
    min_to = current_bet + min_raise if current_bet else big_blind
    # A player can raise unless an all-in short of a full raise is all that reopened the
    # action, and only while someone else still has chips behind to answer it.
    opponents_can_respond = any(
        not players[sid]["folded"] and not players[sid]["all_in"] for sid in hand_sids if sid != for_sid
    )
    can_raise = not player["acted"] and max_to > current_bet and opponents_can_respond
    return {
        "can_act": True,
        "to_call": min(to_call, stack),
        "can_check": to_call == 0,
        "can_raise": can_raise,
        "min_raise_to": min(min_to, max_to),
        "max_raise_to": max_to,
        "raise_label": "Bet" if current_bet == 0 else "Raise",
    }


def _public_state(for_sid: str) -> dict:
    own_player = players.get(for_sid, {})
    return {
        "phase": phase,
        "street": STREET_NAMES.get(phase, ""),
        "own_sid": for_sid,
        "own_is_admin": bool(own_player.get("is_admin")),
        "round_number": round_number,
        "message": message,
        "board": board[:],
        "pot": _pot_total() if hand_sids else 0,
        "current_bet": current_bet,
        "turn_sid": turn_sid,
        "small_blind": small_blind(),
        "big_blind": big_blind,
        "pots": [dict(pot) for pot in showdown_pots],
        "options": _own_options(for_sid),
        "dealing": bool(draws.pending),
        **draws.public(bool(own_player.get("is_admin"))),
        **timer.public(),
        "players": [
            {
                **public_seat(sid, player, accounts),
                "in_hand": sid in hand_sids,
                "hole": _visible_hole(sid, for_sid) if sid in hand_sids else [],
                "folded": player["folded"],
                "all_in": player["all_in"],
                "street_bet": player["street_bet"],
                "total_bet": player["total_bet"],
                "last_action": player["last_action"],
                "hand_label": player["hand_label"] if player["showed"] else "",
                "best_cards": player["best_cards"] if player["showed"] else [],
                "won": player["won"],
                "is_dealer": sid == dealer_sid and bool(hand_sids),
                "is_small_blind": sid == small_blind_sid and bool(hand_sids),
                "is_big_blind": sid == big_blind_sid and bool(hand_sids),
                "ready_eligible": _ready_eligible(player),
            }
            for sid, player in players.items()
        ],
        "min_bet": MIN_BET,
        "max_bet": MAX_BET,
    }


def _broadcast() -> None:
    for sid, player in list(players.items()):
        if player["connected"]:
            socketio.emit("poker_state", _public_state(sid), to=sid)


# --- Socket.IO events ------------------------------------------------------------------------

def on_refresh_state():
    with lock:
        sid = request_sid()
        if sid in players and players[sid]["connected"]:
            emit("poker_state", _public_state(sid), to=sid)


def on_toggle_ready(data=None):
    global message
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player or not player["connected"]:
            _error("Sit down at the poker table first.")
            return
        if is_round_in_progress():
            _error("A hand is already in progress.")
            return
        if not _ready_eligible(player):
            _error(f"You need at least {big_blind:,} credits (the big blind) to play a hand.")
            return
        seconds = parse_decision_seconds((data or {}).get("decision_seconds", timer.seconds))
        if seconds is None:
            return
        timer.seconds = seconds
        player["ready"] = not player["ready"]
        message = f"{player['name']} is {'ready' if player['ready'] else 'no longer ready'}."
        _start_if_everyone_ready()
        _broadcast()


def on_action(data):
    global message, current_bet, min_raise
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player or sid != turn_sid or not is_round_in_progress():
            _error("It's not your turn.")
            return
        options = _own_options(sid)
        action = str((data or {}).get("action", "")).lower()
        if action == "allin" and not options["can_raise"]:
            # Nothing to raise into: going all-in just calls (or checks) what's owed.
            action = "check" if options["can_check"] else "call"
        name = player["name"]
        if action == "fold":
            player["folded"] = True
            player["status"] = "folded"
            player["last_action"] = "Fold"
            message = f"{name} folds."
        elif action == "check":
            if not options["can_check"]:
                _error(f"You need to call {options['to_call']:,} or fold.")
                return
            player["last_action"] = "Check"
            message = f"{name} checks."
        elif action == "call":
            if options["can_check"]:
                _error("There is nothing to call. Check instead.")
                return
            if not _commit(sid, options["to_call"]):
                return
            player["last_action"] = "All-in" if player["all_in"] else f"Call {options['to_call']:,}"
            message = f"{name} {'calls all-in' if player['all_in'] else 'calls'} {options['to_call']:,}."
        elif action in {"raise", "allin"}:
            if not options["can_raise"]:
                _error("You can't raise right now.")
                return
            if action == "allin":
                target = options["max_raise_to"]
            else:
                try:
                    target = int((data or {}).get("amount"))
                except (TypeError, ValueError):
                    _error("Enter a whole-number amount.")
                    return
            if target > options["max_raise_to"]:
                _error(f"You can bet at most {options['max_raise_to']:,} this street.")
                return
            if target < options["min_raise_to"] and target != options["max_raise_to"]:
                minimum = f"bet is {options['min_raise_to']:,}" if current_bet == 0 else f"raise is to {options['min_raise_to']:,}"
                _error(f"The minimum {minimum}.")
                return
            previous_bet = current_bet
            if not _commit(sid, target - player["street_bet"]):
                return
            raise_size = target - previous_bet
            current_bet = max(current_bet, target)
            if raise_size >= min_raise:
                # A full raise reopens the betting for everyone who already acted.
                min_raise = raise_size
                for other_sid in hand_sids:
                    if other_sid != sid:
                        players[other_sid]["acted"] = False
            verb = "bets" if previous_bet == 0 else "raises to"
            player["last_action"] = "All-in" if player["all_in"] else f"{options['raise_label']} {target:,}"
            message = f"{name} {verb} {target:,}{' (all-in)' if player['all_in'] else ''}."
        else:
            _error("Choose fold, check, call, or raise.")
            return
        player["acted"] = True
        _advance()
        _broadcast()

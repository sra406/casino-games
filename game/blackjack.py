"""Server-authoritative Blackjack game engine and Socket.IO handlers."""

from __future__ import annotations

import uuid
from collections import deque

from flask_socketio import emit

from game.common import (
    MAX_BET, MIN_BET, DecisionTimer, ManualDraw, card_rank as _rank, error as _error, everyone_ready, new_seat,
    parse_bet, parse_decision_seconds, public_seat, register_handlers, run_decision_timer,
    save_bet_preference, seat_refusal, set_credit_balance, set_profile_image_version, shuffled_cards,
    stack_card,
)


ROUND_PHASES = ("dealing", "playing", "dealer")
DECKS_PER_SHOE = 6

players: dict[str, dict] = {}
shoe: deque[str] = deque()
dealer_hand: list[str] = []
phase = "waiting"  # waiting, dealing, playing, dealer, round_over
turn_sid: str | None = None
turn_hand_index: int | None = None
message = "Join the table to play."
round_number = 0
active_round_id: str | None = None
timer = DecisionTimer()

app = None
socketio = None
accounts = None
lock = None
request_sid = None


def register(app_instance, socketio_instance, account_service, shared_lock, request_sid_func) -> None:
    """Inject shared services and register this game's socket event handlers."""
    global app, socketio, accounts, lock, request_sid
    app = app_instance
    socketio = socketio_instance
    accounts = account_service
    lock = shared_lock
    request_sid = request_sid_func
    register_handlers(socketio, {
        "refresh_state": on_refresh_state,
        "set_bet": on_set_bet,
        "toggle_ready": on_toggle_ready,
        "hit": on_hit,
        "stand": on_stand,
        "double_down": on_double_down,
        "split": on_split,
    })


def is_round_in_progress() -> bool:
    return phase in ROUND_PHASES


def update_profile_image_version(sid: str, version: int) -> None:
    with lock:
        if set_profile_image_version(players, sid, version):
            _broadcast()


def update_credit_balance(account_id: int, credits: int) -> None:
    with lock:
        changed = set_credit_balance(players, account_id, credits)
        for player in changed:
            if credits < player["bet"]:
                player["ready"] = False
        if changed:
            _broadcast()


def _shoe() -> deque[str]:
    """The live shoe, reshuffled from DECKS_PER_SHOE fresh decks once it runs out."""
    global shoe
    if not shoe:
        shoe = shuffled_cards(DECKS_PER_SHOE)
    return shoe


def _value(hand: list[str]) -> int:
    total = 0
    aces = 0
    for card in hand:
        rank = _rank(card)
        if rank in ("J", "Q", "K"):
            total += 10
        elif rank == "A":
            total += 11
            aces += 1
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _is_blackjack(hand: list[str]) -> bool:
    return len(hand) == 2 and _value(hand) == 21


def _current_player() -> dict | None:
    return players.get(turn_sid) if turn_sid else None


def _current_hand() -> dict | None:
    player = _current_player()
    if player is None or turn_hand_index is None:
        return None
    hands = player.get("hands", [])
    return hands[turn_hand_index] if 0 <= turn_hand_index < len(hands) else None


def _reset_decision_timer() -> None:
    timer.reset(phase == "playing" and turn_sid is not None)


def _can_split(hand: dict) -> bool:
    cards = hand["cards"]
    return hand["status"] == "playing" and len(cards) == 2 and _rank(cards[0]) == _rank(cards[1])


def _advance_turn() -> None:
    """Move to the next player who still needs to act, or play the dealer."""
    global turn_sid, turn_hand_index, phase, message
    hands_in_order = [
        (sid, hand_index)
        for sid, player in players.items()
        for hand_index, hand in enumerate(player.get("hands", []))
        if hand["status"] == "playing"
    ]
    current = (turn_sid, turn_hand_index)
    try:
        current_index = hands_in_order.index(current)
        candidates = hands_in_order[current_index + 1 :] + hands_in_order[:current_index]
    except ValueError:
        candidates = hands_in_order
    if candidates:
        turn_sid, turn_hand_index = candidates[0]
        player = players[turn_sid]
        message = f"{player['name']}'s hand {turn_hand_index + 1} of {len(player['hands'])}."
        _reset_decision_timer()
        return
    turn_sid = None
    turn_hand_index = None
    _reset_decision_timer()
    _play_dealer()


def _play_dealer() -> None:
    global phase, message, turn_sid, turn_hand_index
    phase = "dealer"
    turn_sid = None
    turn_hand_index = None
    if _value(dealer_hand) < 17:
        draws.add({"kind": "dealer"})
        return
    _finish_round()


def _finish_round() -> None:
    global phase, message, active_round_id
    dealer_total = _value(dealer_hand)
    payouts: dict[int, int] = {}
    for player in players.values():
        hand_results = []
        round_credit_change = 0
        for hand in player.get("hands", []):
            total = _value(hand["cards"])
            bet = hand["bet"]
            natural = not hand["from_split"] and _is_blackjack(hand["cards"])
            if hand["status"] == "disconnected":
                result, payout, outcome = "Disconnected — bet lost", 0, "loss"
            elif hand["status"] == "bust":
                result, payout, outcome = "Bust — bet lost", 0, "loss"
            elif natural and _is_blackjack(dealer_hand):
                result, payout, outcome = "Push — both blackjack", bet, "push"
            elif natural:
                result, payout, outcome = "Blackjack! Paid 3:2", bet * 5 // 2, "blackjack"
            elif _is_blackjack(dealer_hand):
                result, payout, outcome = "Dealer blackjack", 0, "loss"
            elif dealer_total > 21 or total > dealer_total:
                result, payout, outcome = "Win — paid 1:1", bet * 2, "win"
            elif total == dealer_total:
                result, payout, outcome = "Push — bet returned", bet, "push"
            else:
                result, payout, outcome = "Dealer wins", 0, "loss"
            hand["result"] = result
            hand["outcome"] = outcome
            hand["credit_change"] = payout - bet
            round_credit_change += hand["credit_change"]
            hand_results.append(result)
            payouts[player["account_id"]] = payouts.get(player["account_id"], 0) + payout
        player["result"] = " · ".join(hand_results)
        player["round_credit_change"] = round_credit_change if hand_results else None
        player["status"] = "round_over"
    balances = accounts.settle_round_payouts(active_round_id, list(payouts.items()))
    for player in players.values():
        if player["account_id"] in balances:
            player["credits"] = balances[player["account_id"]]
    phase = "round_over"
    active_round_id = None
    timer.reset(False)
    message = "Round complete. Start another round when you're ready."


def _finish_deal() -> None:
    global phase, turn_sid, turn_hand_index, message
    for player in players.values():
        if not player["connected"]:
            continue
        for hand in player.get("hands", []):
            if _is_blackjack(hand["cards"]):
                hand["status"] = "blackjack"
    phase = "playing"
    if _is_blackjack(dealer_hand):
        message = "Dealer checks and has blackjack."
        _reset_decision_timer()
        _play_dealer()
        return
    for sid, player in players.items():
        for index, hand in enumerate(player.get("hands", [])):
            if player["connected"] and hand["status"] == "playing":
                turn_sid, turn_hand_index = sid, index
                message = f"{player['name']}'s hand {index + 1} of {len(player['hands'])}."
                _reset_decision_timer()
                return
    turn_sid = None
    turn_hand_index = None
    _reset_decision_timer()
    _play_dealer()


def _finish_split(sid: str, first_index: int) -> None:
    global turn_sid, turn_hand_index, message
    player = players.get(sid)
    if player is None or not player["connected"]:
        return
    for hand in player["hands"][first_index : first_index + 2]:
        if not hand["split_aces"] and _value(hand["cards"]) == 21:
            hand["status"] = "stood"
    active_indices = [
        index for index, hand in enumerate(player["hands"])
        if hand["status"] == "playing"
    ]
    if active_indices:
        turn_sid = sid
        turn_hand_index = active_indices[0]
        message = f"{player['name']} split the pair. Playing hand {turn_hand_index + 1}."
        _reset_decision_timer()
    else:
        turn_sid = sid
        turn_hand_index = first_index
        message = f"{player['name']} split the pair. Both hands have 21."
        _advance_turn()


def _apply_draw(step: dict, card: str) -> None:
    global message
    kind = step["kind"]
    if kind == "deal_player":
        player = players.get(step["sid"])
        if player and player["connected"]:
            player["hands"][0]["cards"].append(card)
    elif kind == "deal_dealer":
        dealer_hand.append(card)
    elif kind in {"hit", "double"}:
        player = players.get(step["sid"])
        hand = player["hands"][step["hand_index"]]
        hand["cards"] = [*hand["cards"], card]
        total = _value(hand["cards"])
        hand_number = step["hand_index"] + 1
        if kind == "double":
            hand["status"] = "bust" if total > 21 else "stood"
            message = f"{player['name']} doubled down, drew {card}, and finished hand {hand_number} at {total}."
            _advance_turn()
        elif total > 21:
            hand["status"] = "bust"
            message = f"{player['name']} drew {card}; hand {hand_number} busts at {total}."
            _advance_turn()
        elif total == 21:
            hand["status"] = "stood"
            message = f"{player['name']} drew {card}; hand {hand_number} has 21."
            _advance_turn()
        else:
            message = f"{player['name']} drew {card}; hand {hand_number} totals {total}. Hit again or stand."
            _reset_decision_timer()
    elif kind == "split_card":
        player = players.get(step["sid"])
        if player and player["connected"]:
            player["hands"][step["hand_index"]]["cards"].append(card)
    elif kind == "dealer":
        dealer_hand.append(card)
        if _value(dealer_hand) >= 17:
            _finish_round()
        else:
            draws.add_next({"kind": "dealer"})


def _run_draw_step(step: dict, card: str | None) -> None:
    """Run one queued step: deal its card, or finish dealing a round or split."""
    if card is not None:
        _apply_draw(step, card)
        if step["kind"] == "dealer" or (step["kind"] == "deal_dealer" and step.get("card_number") == 1):
            _broadcast()
            if step["kind"] == "dealer" and not draws.enabled:
                socketio.sleep(0.18)
    elif step["kind"] == "finish_deal":
        _finish_deal()
    elif step["kind"] == "finish_split":
        _finish_split(step["sid"], step["hand_index"])


def _draw_target(step: dict | None) -> str:
    if not step:
        return ""
    kind = step["kind"]
    if kind == "deal_player":
        player = players.get(step["sid"])
        return f"Deal card {step['card_number']} to {player['name']}" if player else "Deal to player"
    if kind == "deal_dealer":
        return f"Dealer card {step['card_number']}"
    if kind in {"hit", "double", "split_card"}:
        player = players.get(step["sid"])
        if player:
            action = {"hit": "Hit", "double": "Double down", "split_card": "Split deal"}[kind]
            return f"{action}: {player['name']}, hand {step['hand_index'] + 1}"
    if kind == "dealer":
        return "Dealer draw"
    return kind.replace("_", " ")


draws = ManualDraw(_shoe, _run_draw_step, _draw_target)


def _public_state(for_sid: str) -> dict:
    reveal_dealer = phase in ("dealer", "round_over")
    own_player = players.get(for_sid, {})
    active_hand = _current_hand() if turn_sid == for_sid else None
    return {
        "phase": phase,
        "turn_sid": turn_sid,
        "own_sid": for_sid,
        "own_is_admin": bool(own_player.get("is_admin")),
        "own_account_type": own_player.get("account_type"),
        **draws.public(bool(own_player.get("is_admin"))),
        **timer.public(),
        "own_hand_index": turn_hand_index if turn_sid == for_sid else None,
        "own_can_hit": bool(not draws.pending and active_hand and active_hand["status"] == "playing" and not active_hand["split_aces"]),
        "own_can_double": bool(
            not draws.pending and active_hand and active_hand["status"] == "playing" and len(active_hand["cards"]) == 2
            and own_player.get("credits", 0) >= active_hand["bet"]
        ),
        "own_can_split": bool(
            not draws.pending and active_hand and _can_split(active_hand)
            and own_player.get("credits", 0) >= active_hand["bet"]
        ),
        "message": message,
        "round_number": round_number,
        "dealer_hand": dealer_hand[:] if reveal_dealer else (dealer_hand[:1] + (["🂠"] if len(dealer_hand) > 1 else [])),
        "dealer_total": _value(dealer_hand if reveal_dealer else dealer_hand[:1]) if dealer_hand else None,
        "players": [
            {
                **public_seat(sid, player, accounts),
                "hands": [
                    {
                        "cards": hand["cards"][:],
                        "total": _value(hand["cards"]),
                        "bet": hand["bet"],
                        "status": hand["status"],
                        "result": hand.get("result", ""),
                        "outcome": hand.get("outcome"),
                        "credit_change": hand.get("credit_change"),
                    }
                    for hand in player.get("hands", [])
                ],
                "active_hand_index": turn_hand_index if turn_sid == sid else None,
                "result": player["result"],
            }
            for sid, player in players.items()
        ],
        "min_bet": MIN_BET,
        "max_bet": MAX_BET,
    }


def _broadcast() -> None:
    for sid, player in list(players.items()):
        if player["connected"]:
            socketio.emit("state", _public_state(sid), to=sid)


def _stand_on_timeout() -> None:
    """Stand the deciding hand once its timer runs out."""
    global message
    player = _current_player()
    hand = _current_hand()
    if player and hand and hand["status"] == "playing":
        timeout_notice = f"Time expired. Hand {turn_hand_index + 1} stands for {player['name']}."
        message = timeout_notice
        hand["status"] = "stood"
        socketio.emit("notice", {"text": timeout_notice}, to=turn_sid)
        _advance_turn()
        draws.pump()


def decision_timer_loop() -> None:
    """Background task (started by the server) that runs the decision timer."""
    run_decision_timer(
        socketio, lock, timer,
        is_waiting=lambda: phase == "playing" and turn_sid is not None and not draws.pending,
        on_expire=_stand_on_timeout,
        broadcast=_broadcast,
    )


def attach_player(sid: str, account: dict) -> bool:
    global message
    if is_round_in_progress():
        _error("A round is in progress. Sign in after it ends.")
        return False
    for old_sid in [old_sid for old_sid, p in players.items() if not p["connected"] and old_sid != sid]:
        players.pop(old_sid, None)
    refusal = seat_refusal(players, sid, account)
    if refusal:
        _error(refusal)
        return False
    players[sid] = new_seat(account, hands=[], status="waiting", result="")
    message = f"{account['username']} joined the table."
    _broadcast()
    return True


def _ready_eligible(player: dict) -> bool:
    return player["connected"] and player["credits"] >= player["bet"]


def on_refresh_state():
    """Refresh this browser's table view without disconnecting its player."""
    with lock:
        sid = request_sid()
        if sid in players and players[sid]["connected"]:
            emit("state", _public_state(sid), to=sid)


def handle_admin_command(data):
    global message
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
                _error("A round is already in progress.")
                return
            message = f"Admin {player['name']} forced the round to start."
            _begin_round(seconds)
            return
        if command == "/timer":
            blocked = None
            if phase != "playing" or turn_sid is None:
                blocked = "There is no active player decision to update."
            elif draws.pending:
                blocked = "A card draw is pending; set the timer after it is confirmed."
            ok, notice = timer.admin_command(parts, blocked)
            if not ok:
                _error(notice)
                return
            socketio.emit("notice", {"text": notice}, to=sid)
            _broadcast()
            return
        if command == "/skip":
            if len(parts) != 1:
                _error("Usage: /skip")
                return
            if phase != "playing" or turn_sid is None:
                _error("There is no player currently deciding.")
                return
            if draws.pending:
                _error("A card draw is pending. Confirm or cancel it before skipping.")
                return
            current_player = _current_player()
            current_hand = _current_hand()
            if current_player is None or current_hand is None or current_hand["status"] != "playing":
                _error("There is no player currently deciding.")
                return
            hand_number = turn_hand_index + 1
            current_hand["status"] = "stood"
            skipped_notice = f"Admin skipped {current_player['name']}'s decision; hand {hand_number} stands."
            message = skipped_notice
            _advance_turn()
            draws.pump()
            socketio.emit("notice", {"text": skipped_notice}, to=sid)
            _broadcast()
            return
        draw_result = draws.admin_command(command, parts)
        if draw_result is not None:
            ok, notice = draw_result
            if not ok:
                _error(notice)
                return
            socketio.emit("notice", {"text": notice}, to=sid)
            _broadcast()
            return
        if command == "/stackcard":
            if len(parts) != 2:
                _error("Usage: /stackcard rank[suit], for example /stackcard A or /stackcard 10H")
                return
            card = stack_card(_shoe(), parts[1])
            if card is None:
                _error("That value or suit was not found in the remaining deck.")
                return
            socketio.emit("notice", {"text": f"Moved {card} to the top of the deck."}, to=sid)
            _broadcast()
            return
        _error("Commands: /forcestart [seconds] · /skip · /timer seconds|pause|resume · /manualdraw on|off · /draw · /stackcard rank[suit]")


def on_set_bet(data):
    with lock:
        player = players.get(request_sid())
        if not player:
            _error("Join the table first.")
            return
        if is_round_in_progress():
            _error("Bets are locked during a round.")
            return
        bet = parse_bet(data)
        if bet is None:
            return
        if bet > player["credits"]:
            _error("Your bet can't exceed your credits.")
            return
        if save_bet_preference(app, accounts, player, bet):
            player["ready"] = False
            _broadcast()


def on_toggle_ready(data=None):
    """Toggle the requesting player's readiness and start once every eligible seat is ready."""
    global message
    with lock:
        sid = request_sid()
        player = players.get(sid)
        if not player or not player["connected"]:
            _error("Join the table first.")
            return
        if is_round_in_progress():
            _error("A round is already in progress.")
            return
        if player["credits"] < player["bet"]:
            _error("You need enough credits for your bet before readying up.")
            return
        requested_seconds = parse_decision_seconds((data or {}).get("decision_seconds", timer.seconds))
        if requested_seconds is None:
            return
        player["ready"] = not player.get("ready", False)
        if player["ready"]:
            message = f"{player['name']} is ready."
        else:
            message = f"{player['name']} is no longer ready."
        _broadcast()
        if everyone_ready(players, _ready_eligible):
            _begin_round(requested_seconds)


def _begin_round(requested_seconds: int) -> None:
    global phase, turn_sid, turn_hand_index, dealer_hand, message, round_number, active_round_id
    for sid in [sid for sid, player in players.items() if not player["connected"]]:
        players.pop(sid, None)
    active_players = [p for p in players.values() if _ready_eligible(p)]
    if not active_players:
        _error("No players have enough credits for their selected bet.")
        return
    round_id = uuid.uuid4().hex
    try:
        balances = accounts.debit_round_bets(
            round_id,
            [(player["account_id"], player["bet"]) for player in active_players],
        )
    except Exception:
        app.logger.exception("Could not debit blackjack wagers")
        _error("Could not start the round because account storage failed.")
        return
    active_round_id = round_id
    timer.seconds = requested_seconds
    timer.reset(False)
    round_number += 1
    dealer_hand = []
    draws.clear()
    for player in players.values():
        player["hands"] = []
        player["result"] = ""
        player["round_credit_change"] = None
        player["ready"] = False
        if _ready_eligible(player):
            player["credits"] = balances[player["account_id"]]
            player["status"] = "playing"
            player["hands"] = [{
                "cards": [], "bet": player["bet"], "status": "playing",
                "from_split": False, "split_aces": False, "doubled": False,
            }]
        else:
            player["status"] = "waiting"
    active_sids = [sid for sid, player in players.items() if player["status"] == "playing"]
    for card_number in range(1, 3):
        for sid in active_sids:
            draws.add({"kind": "deal_player", "sid": sid, "card_number": card_number})
        draws.add({"kind": "deal_dealer", "card_number": card_number})
    draws.add({"kind": "finish_deal", "draws": False})
    phase = "dealing"
    turn_sid = None
    turn_hand_index = None
    message = f"Dealing round {round_number}."
    draws.pump()
    _broadcast()


def on_hit():
    global message
    with lock:
        sid = request_sid()
        player = _current_player()
        hand = _current_hand()
        if phase != "playing" or draws.pending or sid != turn_sid or not player or not hand:
            _error("It's not your turn.")
            return
        if hand["split_aces"]:
            _error("You can't hit split aces. Stand, double down, or resplit the pair.")
            return
        draws.add({"kind": "hit", "sid": sid, "hand_index": turn_hand_index})
        message = f"Waiting to draw for {player['name']}."
        draws.pump()
        _broadcast()


def on_stand():
    global message
    with lock:
        sid = request_sid()
        player = _current_player()
        hand = _current_hand()
        if phase != "playing" or draws.pending or sid != turn_sid or not player or not hand:
            _error("It's not your turn.")
            return
        hand["status"] = "stood"
        message = f"{player['name']} stands on hand {turn_hand_index + 1}."
        _advance_turn()
        draws.pump()
        _broadcast()


def on_double_down():
    global message
    with lock:
        sid = request_sid()
        player = _current_player()
        hand = _current_hand()
        if phase != "playing" or draws.pending or sid != turn_sid or not player or not hand:
            _error("It's not your turn.")
            return
        if hand["status"] != "playing" or len(hand["cards"]) != 2:
            _error("Double down is available only on a hand's first two cards.")
            return
        try:
            player["credits"] = accounts.add_round_wager(active_round_id, player["account_id"], hand["bet"])
        except ValueError:
            _error("You need enough credits to match this hand's bet.")
            return
        except Exception:
            app.logger.exception("Could not record double-down wager")
            _error("Could not double down because account storage failed.")
            return
        hand["bet"] *= 2
        hand["doubled"] = True
        draws.add({"kind": "double", "sid": sid, "hand_index": turn_hand_index})
        message = f"Waiting for {player['name']}'s double-down card."
        draws.pump()
        _broadcast()


def on_split():
    global message, turn_hand_index
    with lock:
        sid = request_sid()
        player = _current_player()
        hand = _current_hand()
        if phase != "playing" or draws.pending or sid != turn_sid or not player or not hand:
            _error("It's not your turn.")
            return
        if not _can_split(hand):
            _error("Split is available only on a pair among the hand's first two cards.")
            return
        wager = hand["bet"]
        try:
            player["credits"] = accounts.add_round_wager(active_round_id, player["account_id"], wager)
        except ValueError:
            _error("You need enough credits to match this hand's bet.")
            return
        except Exception:
            app.logger.exception("Could not record split wager")
            _error("Could not split because account storage failed.")
            return

        first_card, second_card = hand["cards"]
        split_aces = hand["split_aces"] or _rank(first_card) == "A"
        new_hands = [
            {"cards": [first_card], "bet": wager, "status": "playing", "from_split": True,
             "split_aces": split_aces, "doubled": False, "result": ""},
            {"cards": [second_card], "bet": wager, "status": "playing", "from_split": True,
             "split_aces": split_aces, "doubled": False, "result": ""},
        ]
        player["hands"][turn_hand_index : turn_hand_index + 1] = new_hands
        player["status"] = "playing"
        first_index = turn_hand_index
        draws.add(
            {"kind": "split_card", "sid": sid, "hand_index": first_index},
            {"kind": "split_card", "sid": sid, "hand_index": first_index + 1},
            {"kind": "finish_split", "sid": sid, "hand_index": first_index, "draws": False},
        )
        message = f"Waiting to deal the split hands for {player['name']}."
        draws.pump()
        _broadcast()


def disconnect_player(sid: str) -> None:
    global message
    with lock:
        player = players.get(sid)
        if not player:
            return
        player["connected"] = False
        if is_round_in_progress():
            player["status"] = "disconnected"
            for hand in player.get("hands", []):
                hand["status"] = "disconnected"
            draws.drop(lambda step: step.get("sid") == sid)
            if turn_sid == sid:
                _advance_turn()
        else:
            players.pop(sid, None)
            message = f"{player['name']} left the table."
        if player["is_admin"] and draws.disable():
            message = "Manual draw mode disabled because the admin disconnected."
        if is_round_in_progress():
            draws.pump()
        elif everyone_ready(players, _ready_eligible):
            _begin_round(timer.seconds)
        _broadcast()


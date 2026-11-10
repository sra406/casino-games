"""Shared account, profile picture, and account administration services."""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from flask import Response, abort
from flask_socketio import emit


MAX_PROFILE_IMAGE_MB = 5
MAX_PROFILE_IMAGE_BYTES = MAX_PROFILE_IMAGE_MB * 1024 * 1024
# Base64 inflates uploads by 4/3; this is the longest data URL a valid image can produce.
MAX_PROFILE_DATA_URL_CHARS = 4 * ((MAX_PROFILE_IMAGE_BYTES + 2) // 3) + 32
PROFILE_IMAGE_TOO_LARGE = f"Profile pictures must be {MAX_PROFILE_IMAGE_MB} MB or smaller."
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{3,20}")
ADMIN_PERMISSION = "admin.access"


def valid_username(username: str) -> bool:
    return bool(USERNAME_PATTERN.fullmatch(username))


@dataclass(frozen=True)
class GameAccountAdapter:
    """Game callbacks needed to connect shared accounts to a game's table."""

    join: Callable[[str, dict], bool]
    is_round_in_progress: Callable[[], bool]
    disconnect: Callable[[str], None]
    profile_updated: Callable[[str, int], None]
    update_credits: Callable[[int, int], None]
    admin_command: Callable[[dict], None]
    # Games that can seat a newcomer during a round (to play from the next one).
    joins_mid_round: bool = False

    @classmethod
    def from_module(cls, module) -> "GameAccountAdapter":
        """Build an adapter from a game module exposing the standard hook functions."""
        return cls(
            join=module.attach_player,
            is_round_in_progress=module.is_round_in_progress,
            disconnect=module.disconnect_player,
            profile_updated=module.update_profile_image_version,
            update_credits=module.update_credit_balance,
            admin_command=module.handle_admin_command,
            joins_mid_round=getattr(module, "JOINS_MID_ROUND", False),
        )


class AccountService:
    def __init__(self, app, socketio, database, lock, request_sid):
        self.app = app
        self.socketio = socketio
        self.database = database
        self.lock = lock
        self.request_sid = request_sid
        self.games: dict[str, GameAccountAdapter] = {}
        self.connected_accounts: dict[str, dict] = {}
        self.resume_tokens: dict[str, dict] = {}
        self.pending_temporary_cleanup: dict[int, str] = {}
        self.temporary_handoff_seconds = 15
        # Set by AdminConsole so it can follow session changes.
        self.admin_console = None

    def initialize(self, admin_username: str, admin_password: str) -> tuple[int, int]:
        self.database.init_db()
        removed_temporary = self.database.cleanup_temporary_accounts()
        refunded_rounds = self.database.refund_incomplete_rounds()
        if not admin_username or not admin_password:
            raise RuntimeError("Set both ADMIN_USERNAME and ADMIN_PASSWORD before starting the server")
        if not valid_username(admin_username) or not 10 <= len(admin_password) <= 128:
            raise RuntimeError("Admin username must be 3–20 letters/numbers/underscores and password 10–128 characters")
        self.database.bootstrap_admin_account(admin_username, admin_password)
        return removed_temporary, refunded_rounds

    def shutdown(self) -> None:
        try:
            deleted = self.database.cleanup_temporary_accounts()
            if deleted:
                print(f"Removed {deleted} temporary account(s).")
        except Exception:
            self.app.logger.exception("Could not clear temporary accounts during shutdown")

    def register(self, games: dict[str, GameAccountAdapter]) -> None:
        """Register shared account routes and Socket.IO events once per app."""
        self.games = dict(games)
        self.app.add_url_rule(
            "/profile-image/<int:account_id>",
            endpoint="shared_profile_image",
            view_func=self.serve_profile_image,
            methods=["GET"],
        )
        handlers = {
            "register": self.on_register,
            "temporary": self.on_temporary,
            "login": self.on_login,
            "resume_account": self.on_resume_account,
            "profile_picture": self.on_profile_picture,
            "admin_command": self.on_admin_command,
            "disconnect": self.on_disconnect,
        }
        for event_name, handler in handlers.items():
            self.socketio.on_event(event_name, handler)

    def profile_image_url(self, account_id: int, version: int) -> str | None:
        if not version:
            return None
        return f"/profile-image/{account_id}?v={version}"

    def update_bet_preference(self, account_id: int, bet: int) -> None:
        self.database.update_bet(account_id, bet)

    def debit_round_bets(self, round_id: str, wagers: list[tuple[int, int]]) -> dict[int, int]:
        return self.database.debit_round_bets(round_id, wagers)

    def settle_round_payouts(self, round_id: str, payouts: list[tuple[int, int]],
                             carried: list[tuple[int, int]] = ()) -> dict[int, int]:
        return self.database.settle_round_payouts(round_id, payouts, carried)

    def return_round_wager(self, round_id: str, account_id: int, refund: int, forfeit: int = 0) -> int:
        return self.database.return_round_wager(round_id, account_id, refund, forfeit)

    def add_round_wager(self, round_id: str, account_id: int, amount: int) -> int:
        return self.database.add_round_wager(round_id, account_id, amount)

    def _valid_credentials(self, data: dict | None) -> tuple[str, str] | None:
        username = str((data or {}).get("username", "")).strip()
        password = str((data or {}).get("password", ""))
        if not valid_username(username):
            self._error("Username must be 3–20 letters, numbers, or underscores.")
            return None
        if not 10 <= len(password) <= 128:
            self._error("Password must be between 10 and 128 characters.")
            return None
        return username, password

    def _decode_profile_image(self, data: dict | None) -> tuple[bytes, str]:
        data_url = data.get("data_url", "") if isinstance(data, dict) else ""
        if not isinstance(data_url, str) or len(data_url) > MAX_PROFILE_DATA_URL_CHARS:
            raise ValueError(PROFILE_IMAGE_TOO_LARGE)
        match = re.fullmatch(
            r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]*={0,2})",
            data_url,
        )
        if not match:
            raise ValueError("Choose a JPEG, PNG, or WebP image.")
        mime_type, encoded = match.groups()
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("The selected image could not be read.") from None
        if not image or len(image) > MAX_PROFILE_IMAGE_BYTES:
            raise ValueError(PROFILE_IMAGE_TOO_LARGE)
        signatures = {
            "image/jpeg": image.startswith(b"\xff\xd8\xff"),
            "image/png": image.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/webp": image.startswith(b"RIFF") and image[8:12] == b"WEBP",
        }
        if not signatures[mime_type]:
            raise ValueError("The selected file does not match its image type.")
        return image, mime_type

    def _error(self, text: str) -> None:
        emit("notice", {"text": text})

    def is_admin(self, account_id: int) -> bool:
        """Permission check shared by game admin panels and the admin terminal."""
        try:
            return bool(self.database.has_permission(account_id, ADMIN_PERMISSION))
        except Exception:
            self.app.logger.exception("Could not verify admin permission")
            return False

    def _game_for(self, data: dict | None) -> tuple[str, GameAccountAdapter] | None:
        game_name = str((data or {}).get("game", "blackjack")).strip().lower()
        adapter = self.games.get(game_name)
        if adapter is None:
            self._error("That game is not available for account sign-in yet.")
            return None
        if adapter.is_round_in_progress() and not adapter.joins_mid_round:
            self._error("A round is in progress. Join after it ends.")
            return None
        return game_name, adapter

    def _attach(
        self,
        sid: str,
        account: dict,
        game_name: str,
        adapter: GameAccountAdapter,
        session_token: str | None = None,
        delete_temporary_on_fail: bool = True,
    ) -> bool:
        account_context = dict(account)
        account_context["profile_image_version"] = self.database.get_profile_image_version(account["id"])
        account_context["is_admin"] = self.is_admin(account["id"])
        if not adapter.join(sid, account_context):
            if delete_temporary_on_fail and account.get("account_type") == "temporary":
                self.database.delete_temporary_account(account["id"])
            return False
        token = session_token or secrets.token_urlsafe(32)
        now = time.time()
        self.resume_tokens = {
            key: value for key, value in self.resume_tokens.items()
            if value["expires_at"] > now
        }
        self.resume_tokens[token] = {
            "account_id": account["id"],
            "expires_at": now + 7 * 24 * 60 * 60,
        }
        self.connected_accounts[sid] = {
            "account_id": account["id"],
            "account_type": account["account_type"],
            "username": account["username"],
            "game": game_name,
            "resume_token": token,
            "connected_at": now,
        }
        self.pending_temporary_cleanup.pop(account["id"], None)
        self.socketio.emit("account_session", {"token": token}, to=sid)
        self._sessions_changed()
        return True

    def _sessions_changed(self) -> None:
        if self.admin_console is not None:
            self.admin_console.push_players()

    def account_for_resume_token(self, token: str) -> int | None:
        """Return the account id behind a live resume token, if any."""
        session = self.resume_tokens.get(token)
        if not token or not session or session["expires_at"] <= time.time():
            return None
        return session["account_id"]

    def connected_players(self) -> list[dict]:
        """Snapshot of every signed-in game connection, for the admin terminal."""
        players = []
        for sid, session in self.connected_accounts.items():
            try:
                account = self.database.get_account_by_id(session["account_id"])
            except Exception:
                self.app.logger.exception("Could not load account for admin player list")
                account = None
            players.append({
                "sid": sid,
                "username": session["username"],
                "account_type": session["account_type"],
                "game": session["game"],
                "credits": account["credits"] if account else None,
                "connected_at": session.get("connected_at"),
            })
        players.sort(key=lambda player: (player["game"], player["username"].casefold()))
        return players

    def disconnect_username(self, username: str) -> list[dict]:
        """Disconnect every game connection signed in as username; returns those sessions."""
        targets = [
            (sid, dict(session)) for sid, session in self.connected_accounts.items()
            if session["username"].casefold() == username.casefold()
        ]
        for sid, _session in targets:
            self.socketio.emit("admin_disconnected", {"text": "An admin disconnected you from the table."}, to=sid)
            self.socketio.server.disconnect(sid, namespace="/")
        return [session for _sid, session in targets]

    def _sign_in(self, data, load_account, failure: str, **attach_options) -> None:
        """Shared tail of every sign-in path: load the account, then seat it in the game."""
        game = self._game_for(data)
        if not game:
            return
        game_name, adapter = game
        with self.lock:
            try:
                account = load_account()
                if account is None:
                    if failure:
                        self._error(failure)
                    return
                self._attach(self.request_sid(), account, game_name, adapter, **attach_options)
            except Exception:
                self.app.logger.exception("Account sign-in failed")
                self._error("Account storage is unavailable. Please try again shortly.")

    def on_resume_account(self, data):
        sid = self.request_sid()
        token = str((data or {}).get("token", ""))

        def load_account():
            account_id = self.account_for_resume_token(token)
            account = self.database.get_account_by_id(account_id) if account_id is not None else None
            if account is None:
                self.resume_tokens.pop(token, None)
                self.socketio.emit("account_session_invalid", {"token": token}, to=sid)
            return account

        self._sign_in(data, load_account, "", session_token=token, delete_temporary_on_fail=False)

    def on_register(self, data):
        credentials = self._valid_credentials(data)
        if credentials:
            self._sign_in(data, lambda: self.database.create_account(*credentials), "That username is already taken.")

    def on_temporary(self, data):
        username = str((data or {}).get("username", "")).strip()
        if not valid_username(username):
            self._error("Username must be 3–20 letters, numbers, or underscores.")
            return
        self._sign_in(
            data, lambda: self.database.create_temporary_account(username),
            "That username is already in use. Choose another.",
        )

    def on_login(self, data):
        credentials = self._valid_credentials(data)
        if credentials:
            self._sign_in(data, lambda: self.database.authenticate(*credentials), "Username or password is incorrect.")

    def on_profile_picture(self, data):
        sid = self.request_sid()
        with self.lock:
            session = self.connected_accounts.get(sid)
            if not session:
                self._error("Join a game before uploading a profile picture.")
                return
            try:
                image, mime_type = self._decode_profile_image(data)
            except ValueError as exc:
                self._error(str(exc))
                return
            try:
                version = self.database.save_profile_image(session["account_id"], image, mime_type)
                adapter = self.games[session["game"]]
                adapter.profile_updated(sid, version)
            except Exception:
                self.app.logger.exception("Profile picture could not be saved")
                self._error("Could not save your profile picture. Please try again.")
                return
            self.socketio.emit("notice", {"text": "Profile picture updated."}, to=sid)

    def on_admin_command(self, data):
        sid = self.request_sid()
        with self.lock:
            session = self.connected_accounts.get(sid)
            if not session:
                self._error("Sign in with an admin account first.")
                return
            if not self.is_admin(session["account_id"]):
                self._error("You don't have permission to run admin commands.")
                return

            command = str((data or {}).get("command", "")).strip()
            parts = command.split()
            if parts and parts[0].lower() == "/setcredits":
                ok, text = self.set_credits(parts)
                if ok:
                    self.socketio.emit("notice", {"text": text}, to=sid)
                else:
                    self._error(text)
                return

            adapter = self.games.get(session["game"])
            if adapter is None:
                self._error("The current game is unavailable.")
                return
            adapter.admin_command(data or {})

    def set_credits(self, parts: list[str]) -> tuple[bool, str]:
        """Apply `/setcredits username amount`; returns (success, message)."""
        if len(parts) != 3:
            return False, "Usage: /setcredits username amount"
        target_name = parts[1]
        if not valid_username(target_name):
            return False, "Use a valid account username."
        try:
            amount = int(parts[2])
        except ValueError:
            return False, "Credit amount must be a whole number."
        if not 0 <= amount <= 1_000_000_000:
            return False, "Credit amount must be between 0 and 1,000,000,000."
        try:
            target = self.database.set_credits(target_name, amount)
        except Exception:
            self.app.logger.exception("Admin credit update failed")
            return False, "Could not update credits. Try again shortly."
        if target is None:
            return False, "No account found with that username."
        for adapter in self.games.values():
            adapter.update_credits(target["id"], target["credits"])
        self._sessions_changed()
        return True, f"Set {target['username']}'s credits to {amount:,}."

    def on_disconnect(self):
        sid = self.request_sid()
        with self.lock:
            if self.admin_console is not None:
                self.admin_console.forget(sid)
            session = self.connected_accounts.pop(sid, None)
            if not session:
                return
            adapter = self.games.get(session["game"])
            if adapter:
                adapter.disconnect(sid)
            self._sessions_changed()
            if session["account_type"] == "temporary":
                cleanup_id = secrets.token_urlsafe(12)
                self.pending_temporary_cleanup[session["account_id"]] = cleanup_id
                self.socketio.start_background_task(
                    self._cleanup_temporary_after_handoff,
                    session["account_id"],
                    cleanup_id,
                )

    def _cleanup_temporary_after_handoff(self, account_id: int, cleanup_id: str) -> None:
        self.socketio.sleep(self.temporary_handoff_seconds)
        with self.lock:
            if self.pending_temporary_cleanup.get(account_id) != cleanup_id:
                return
            if any(session["account_id"] == account_id for session in self.connected_accounts.values()):
                return
            try:
                self.database.delete_temporary_account(account_id)
            except Exception:
                self.app.logger.exception("Could not delete disconnected temporary account")
                return
            self.resume_tokens = {
                key: value for key, value in self.resume_tokens.items()
                if value["account_id"] != account_id
            }
            self.pending_temporary_cleanup.pop(account_id, None)

    def serve_profile_image(self, account_id: int):
        stored = self.database.get_profile_image(account_id)
        if stored is None:
            abort(404)
        image, mime_type = stored
        return Response(
            image,
            mimetype=mime_type,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

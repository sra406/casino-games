"""Central admin terminal: a live list of every connected player plus table-wide commands."""

from __future__ import annotations

import time

from flask import render_template, request
from flask_socketio import emit

from accounts import valid_username


HELP_LINES = [
    "/players",
    "    list every connected player",
    "/disconnect username",
    "    disconnect that player from their game",
    "/setcredits username amount",
    "    set an account's balance (0–1,000,000,000)",
    "/clear",
    "    clear this terminal",
]


class AdminConsole:
    def __init__(self, app, socketio, account_service, database, lock, request_sid):
        self.app = app
        self.socketio = socketio
        self.accounts = account_service
        self.database = database
        self.lock = lock
        self.request_sid = request_sid
        # Socket ids of admin terminal pages that have signed in.
        self.sessions: dict[str, dict] = {}

    def register(self) -> None:
        self.accounts.admin_console = self
        self.app.add_url_rule("/admin", endpoint="admin_console", view_func=self.page, methods=["GET"])
        handlers = {
            "admin_console_login": self.on_login,
            "admin_console_resume": self.on_resume,
            "admin_console_command": self.on_command,
            "admin_console_refresh": self.on_refresh,
        }
        for event_name, handler in handlers.items():
            self.socketio.on_event(event_name, handler)

    def page(self):
        embedded = request.args.get("embedded") == "1"
        return render_template("admin.html", active_game="admin", embedded=embedded)

    # ------------------------------------------------------------------ helpers

    def _output(self, lines: list[str] | str, level: str = "info", to: str | None = None) -> None:
        payload = {"lines": [lines] if isinstance(lines, str) else lines, "level": level}
        if to is None:
            emit("admin_console_output", payload)
        else:
            self.socketio.emit("admin_console_output", payload, to=to)

    def _open_session(self, sid: str, account: dict) -> None:
        if not self.accounts.is_admin(account["id"]):
            emit("admin_console_denied", {"text": "That account doesn't have admin access."})
            return
        self.sessions[sid] = {"account_id": account["id"], "username": account["username"]}
        emit("admin_console_ready", {"username": account["username"]})
        self._output([f"Signed in as {account['username']}. Type /help for commands."])
        self.push_players(only=sid)

    def _require_admin(self, sid: str) -> dict | None:
        session = self.sessions.get(sid)
        if not session:
            emit("admin_console_denied", {"text": "Sign in with an admin account first."})
            return None
        # Re-check on every action so a revoked permission takes effect immediately.
        if not self.accounts.is_admin(session["account_id"]):
            self.sessions.pop(sid, None)
            emit("admin_console_denied", {"text": "Admin access was revoked for this account."})
            return None
        return session

    def forget(self, sid: str) -> None:
        self.sessions.pop(sid, None)

    def push_players(self, only: str | None = None) -> None:
        targets = [only] if only else list(self.sessions)
        if not targets:
            return
        payload = {"players": self.accounts.connected_players(), "server_time": time.time()}
        for sid in targets:
            if sid in self.sessions:
                self.socketio.emit("admin_players", payload, to=sid)

    # ------------------------------------------------------------------ events

    def on_login(self, data):
        sid = self.request_sid()
        username = str((data or {}).get("username", "")).strip()
        password = str((data or {}).get("password", ""))
        if not valid_username(username) or not 10 <= len(password) <= 128:
            emit("admin_console_denied", {"text": "Username or password is incorrect."})
            return
        with self.lock:
            try:
                account = self.database.authenticate(username, password)
            except Exception:
                self.app.logger.exception("Admin console login failed")
                emit("admin_console_denied", {"text": "Account storage is unavailable. Try again shortly."})
                return
            if account is None:
                emit("admin_console_denied", {"text": "Username or password is incorrect."})
                return
            self._open_session(sid, account)

    def on_resume(self, data):
        """Reuse the game sign-in token this browser tab already holds."""
        sid = self.request_sid()
        token = str((data or {}).get("token", ""))
        with self.lock:
            account_id = self.accounts.account_for_resume_token(token)
            if account_id is None:
                emit("admin_console_denied", {"text": "", "silent": True})
                return
            try:
                account = self.database.get_account_by_id(account_id)
            except Exception:
                self.app.logger.exception("Admin console resume failed")
                account = None
            if not account:
                emit("admin_console_denied", {"text": "", "silent": True})
                return
            self._open_session(sid, account)

    def on_refresh(self, _data=None):
        sid = self.request_sid()
        with self.lock:
            if self._require_admin(sid):
                self.push_players(only=sid)

    def on_command(self, data):
        sid = self.request_sid()
        with self.lock:
            session = self._require_admin(sid)
            if not session:
                return
            command = str((data or {}).get("command", "")).strip()
            parts = command.split()
            name = parts[0].lower() if parts else ""
            if name in {"/help", "help", "?"}:
                self._output(HELP_LINES)
            elif name in {"/players", "/list", "/who"}:
                self._list_players()
            elif name in {"/disconnect", "/kick", "/forcedisconnect"}:
                self._disconnect(session, parts)
            elif name == "/setcredits":
                ok, text = self.accounts.set_credits(parts)
                self._output(text, "success" if ok else "error")
            else:
                self._output(f"Unknown command: {command or '(empty)'}. Type /help for commands.", "error")

    def _list_players(self) -> None:
        players = self.accounts.connected_players()
        if not players:
            self._output("No players are connected.")
            return
        now = time.time()
        lines = [f"{'USERNAME':<20} {'GAME':<10} {'CREDITS':>13}  CONNECTED"]
        for player in players:
            credits = "—" if player["credits"] is None else f"{player['credits']:,}"
            minutes = int((now - (player["connected_at"] or now)) // 60)
            username = player["username"] + ("*" if player["account_type"] == "temporary" else "")
            lines.append(f"{username:<20} {player['game']:<10} {credits:>13}  {minutes} min")
        lines.append(f"{len(players)} connected player{'s' if len(players) != 1 else ''}. * temporary account")
        self._output(lines)

    def _disconnect(self, session: dict, parts: list[str]) -> None:
        if len(parts) != 2:
            self._output("Usage: /disconnect username", "error")
            return
        removed = self.accounts.disconnect_username(parts[1])
        if not removed:
            self._output(f"No connected player is signed in as {parts[1]}.", "error")
            return
        games = ", ".join(sorted({entry["game"] for entry in removed}))
        self._output(f"Disconnected {removed[0]['username']} from {games}.", "success")
        self.app.logger.info("Admin %s disconnected %s", session["username"], removed[0]["username"])
        self.push_players()

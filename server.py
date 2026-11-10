"""Shared web application and account services for the multiplayer games."""

from __future__ import annotations

import atexit
import importlib.util
import os
import signal
import sys
import threading

import database
from flask import Flask, abort, render_template, send_from_directory, url_for
from flask_socketio import SocketIO

from accounts import MAX_PROFILE_DATA_URL_CHARS, MAX_PROFILE_IMAGE_MB, AccountService, GameAccountAdapter
from admin_console import AdminConsole
from game import blackjack as blackjack_game
from game import craps as craps_game
from game import poker as poker_game
from game import roulette as roulette_game
from game.common import MAX_BET, MAX_DECISION_SECONDS, MAX_PLAYERS, MIN_BET, MIN_DECISION_SECONDS


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("BLACKJACK_SECRET", "change-this-local-game-key")
# Socket.IO caps each message at 1 MB by default; profile pictures arrive as base64
# data URLs, so allow the largest valid upload plus room for the event envelope.
socketio = SocketIO(app, async_mode="threading", max_http_buffer_size=MAX_PROFILE_DATA_URL_CHARS + 64 * 1024)
lock = threading.RLock()


APP_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


@app.context_processor
def template_helpers():
    def asset(filename: str) -> str:
        """Static URL versioned by the file's modification time, so edits bust browser caches."""
        try:
            version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)

    return {"asset": asset, "min_bet": MIN_BET, "max_bet": MAX_BET, "max_players": MAX_PLAYERS,
            "max_profile_image_mb": MAX_PROFILE_IMAGE_MB,
            "min_decision_seconds": MIN_DECISION_SECONDS, "max_decision_seconds": MAX_DECISION_SECONDS}


def _serve_first_image(folder: str):
    """Serve the first supported image (by name) in a local folder, never cached stale."""
    directory = os.path.join(APP_DIR, folder)
    try:
        images = sorted(
            (
                entry.name
                for entry in os.scandir(directory)
                if entry.is_file(follow_symlinks=False)
                and os.path.splitext(entry.name)[1].lower() in IMAGE_EXTENSIONS
            ),
            key=str.casefold,
        )
    except FileNotFoundError:
        abort(404)
    if not images:
        abort(404)
    response = send_from_directory(directory, images[0])
    # Check for a replacement image on reload instead of keeping an old copy cached.
    response.headers["Cache-Control"] = "no-cache, max-age=0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
def index():
    return render_template("index.html", active_game="menu")


@app.get("/blackjack")
def blackjack():
    return render_template("games/blackjack.html", active_game="blackjack")


@app.get("/roulette")
def roulette():
    return render_template("games/roulette.html", active_game="roulette")


@app.get("/craps")
def craps():
    return render_template("games/craps.html", active_game="craps")


@app.get("/poker")
def poker():
    return render_template("games/poker.html", active_game="poker")


@app.get("/table-background")
def table_background():
    return _serve_first_image("Background")


@app.get("/refresh-button-image")
def refresh_button_image():
    return _serve_first_image("Refresh")


def request_sid() -> str:
    # Kept in a helper to make the Flask-SocketIO request context explicit.
    from flask import request
    return request.sid


GAMES = {"blackjack": blackjack_game, "roulette": roulette_game, "craps": craps_game, "poker": poker_game}

account_service = AccountService(app, socketio, database, lock, request_sid)
for game_module in GAMES.values():
    game_module.register(app, socketio, account_service, lock, request_sid)
account_service.register({name: GameAccountAdapter.from_module(module) for name, module in GAMES.items()})
admin_console = AdminConsole(app, socketio, account_service, database, lock, request_sid)
admin_console.register()


_services_started = False


def start_services() -> None:
    """Prepare MySQL, the admin account and background timers once per server process.

    Gunicorn calls this from gunicorn.conf.py when its worker boots; the built-in
    development server calls it before serving.
    """
    global _services_started
    if _services_started:
        return
    admin_username = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    removed_temporary, refunded = account_service.initialize(admin_username, admin_password)
    for game_module in GAMES.values():
        if hasattr(game_module, "decision_timer_loop"):
            socketio.start_background_task(game_module.decision_timer_loop)
    _services_started = True
    print(f"Admin account '{admin_username}' is ready.", flush=True)
    print(
        f"MySQL account tables are ready. Cleared {removed_temporary} old temporary account(s); "
        f"refunded interrupted wagers for {refunded} account(s).",
        flush=True,
    )
    print(f"Blackjack, roulette, craps and poker are available on port {os.environ.get('PORT', '8000')}.", flush=True)


def stop_services() -> None:
    if _services_started:
        account_service.shutdown()


def _run_builtin_server() -> None:
    """Fallback for Windows, where Gunicorn is unavailable."""
    start_services()

    def handle_shutdown_signal(_signum, _frame):
        raise SystemExit(0)

    atexit.register(stop_services)
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(shutdown_signal, handle_shutdown_signal)
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    if os.name != "nt" and importlib.util.find_spec("gunicorn") is not None:
        # Replace this process with Gunicorn using the settings in gunicorn.conf.py.
        config = os.path.join(APP_DIR, "gunicorn.conf.py")
        os.execv(sys.executable, [sys.executable, "-m", "gunicorn", "--config", config])
    print("Gunicorn is not available here; using the built-in development server.", flush=True)
    _run_builtin_server()

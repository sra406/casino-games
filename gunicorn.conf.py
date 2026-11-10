"""Gunicorn settings for the game server.

Gunicorn loads this file automatically when started from this folder, so either
`gunicorn` or `python server.py` starts the tables with these settings.
"""

import os

wsgi_app = "server:app"
chdir = os.path.dirname(os.path.abspath(__file__))
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Every table, seat and spin lives in this process's memory, so there must be exactly
# one worker. Threads serve the concurrent Socket.IO connections (one per open page).
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "100"))
# Socket.IO long-polling requests can stay open for up to its ping interval.
timeout = 60
graceful_timeout = 10
accesslog = None
errorlog = "-"


def post_worker_init(worker):
    """Prepare the database, admin account and background timers inside the worker."""
    if worker.cfg.workers != 1:
        raise RuntimeError("Run the game server with exactly one Gunicorn worker; table state is kept in memory.")
    import server as game_server

    game_server.start_services()


def worker_exit(_arbiter, _worker):
    """Clean up accounts, then exit without waiting on open browser connections.

    Each open page holds a WebSocket request on a worker thread, and Python would wait
    for those threads until Gunicorn force-kills the worker. Table state lives in
    memory and the database work is already committed, so nothing is left to finish.
    """
    import sys

    import server as game_server

    try:
        game_server.stop_services()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

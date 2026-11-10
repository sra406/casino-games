# Local Multiplayer Blackjack

A browser-based blackjack table that runs on your PC. Players connect to the same live table using a browser. The server controls the cards and turns, so every player sees the same round.

## Set up MySQL

Install and start MySQL Server on the host PC. In a MySQL command prompt, create a database and a dedicated application user (replace the example password with a private one):

```sql
CREATE DATABASE blackjack CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'blackjack_app'@'localhost' IDENTIFIED BY 'replace-with-a-strong-password';
GRANT ALL PRIVILEGES ON blackjack.* TO 'blackjack_app'@'localhost';
```

The Python app connects to MySQL at `localhost` only and creates its tables on startup. MySQL stores the account records, password hashes, play credits, preferred bets, roles, and permissions. Do not delete the MySQL database if you want to keep accounts.

## Start the game

1. Install Python 3.10 or newer.
2. Open a terminal in this folder and install the packages:

   ```sh
   python -m pip install -r requirements.txt
   ```

3. Configure the MySQL connection in the same terminal where you will start the game. The MySQL host is fixed to `localhost`.

   macOS/Linux:

   ```sh
   export MYSQL_PORT=3306
   export MYSQL_DATABASE=blackjack
   export MYSQL_USER=blackjack_app
   export MYSQL_PASSWORD='your-private-mysql-password'
   ```

   Windows PowerShell:

   ```powershell
   $env:MYSQL_PORT = "3306"
   $env:MYSQL_DATABASE = "blackjack"
   $env:MYSQL_USER = "blackjack_app"
   $env:MYSQL_PASSWORD = "your-private-mysql-password"
   ```

4. Configure the admin account in that same terminal. Choose a unique username and a private password of at least 10 characters.

   macOS/Linux:

   ```sh
   export ADMIN_USERNAME=admin
   export ADMIN_PASSWORD='choose-a-private-admin-password'
   ```

   Windows PowerShell:

   ```powershell
   $env:ADMIN_USERNAME = "admin"
   $env:ADMIN_PASSWORD = "choose-a-private-admin-password"
   ```

   On startup, the app creates that account if needed and grants it the admin role. It sets that account's password from `ADMIN_PASSWORD` each time the server starts, so keep the value secure and consistent.

5. Start the server:

   ```sh
   python server.py
   ```

   On macOS and Linux this starts the game under Gunicorn using the settings in `gunicorn.conf.py`; running `gunicorn` from this folder does the same. Gunicorn prepares the MySQL tables, admin account, and decision timer when its worker boots, and removes temporary accounts when you stop it with Ctrl+C. Keep it at one worker (the default here) because the tables live in that process's memory; `GUNICORN_THREADS` (default 100) caps how many browser pages can be connected at once. On Windows, where Gunicorn isn't available, `python server.py` uses the built-in development server instead.

6. On the host PC, open [http://localhost:8000](http://localhost:8000) and choose **Blackjack**. Sign in as the configured admin, create a permanent account, or choose **Temporary** to join with only a username.
7. For people on your home network, give them the host PC's local IP address, such as `http://192.168.1.25:8000`.

The server listens on all network interfaces at port 8000. To let people outside your home connect, configure your router to forward TCP port **8000** to the host PC's local IP address, and allow Python through the PC's firewall. Give players your public IP address followed by `:8000`, for example `http://203.0.113.12:8000`.

The game picker is at `/`. Blackjack runs at `/blackjack`, roulette at `/roulette`, craps at `/craps`, and Texas Hold'em poker at `/poker`; each game has its own server module under `game/` and browser client under `static/games/`. `accounts.py` centralizes sign-in, registration, temporary accounts, credit transactions, admin account commands, and profile picture upload/serving. All four games use the same MySQL accounts, play-credit balances, preferred bet, and profile pictures through their game adapters. Seat, bet, readiness, playing-card, decision-timer, and manual-draw helpers the tables share live in `game/common.py`; roulette and craps share chip-layout betting (staging, undo, clear, rebet, ready-to-play, delayed result reveal) in `game/chips.py` and the clickable board, chip rack, and seat cards in `static/chip-table.js` with `templates/partials/chip-controls.html`. Every page extends `templates/base.html`; `static/table.js` provides the shared socket, connection badge, notices, refresh button, admin command box, and card/credit/profile rendering helpers, and `static/accounts.js` with `templates/partials/account-panel.html` handles sign-in and profile pictures. `admin_console.py` serves the `/admin` terminal. `server.py` initializes the shared app, versions static files automatically for cache busting, and connects each game module to the account service.

Keep the host PC awake and leave the terminal window running while playing. If the PC's local IP changes, update the router's forwarding rule; reserving a local IP for the PC in the router can help.

## Table rules

- One shared table, up to 8 players.
- Permanent accounts use a unique username and password; new accounts start with 500 play credits and persist in MySQL.
- Signed-in players can upload a JPEG, PNG, or WebP profile picture up to 5 MB. The image is stored in MySQL and shown on the right side of that player's card; temporary-account images are deleted with the account.
- Put a JPG, PNG, WebP, GIF, or AVIF image in the `Background` folder beside `server.py` to use it as the table background for everyone. If the folder contains multiple supported images, the first filename alphabetically is used; replace the image and reload the page to refresh it.
- Put a JPG, PNG, WebP, GIF, or AVIF image in the `Refresh` folder beside `server.py` to use it as the refresh button icon. The first filename alphabetically is used; if the folder is empty, the button shows its default refresh symbol.
- The header's refresh button requests a fresh live table snapshot and reloads the table background and button icon without reloading the page or disconnecting your player, so your sign-in and active hand stay in place.
- Sign-in follows you between Blackjack, Roulette, Craps, and Poker in the same browser tab. The session token expires after seven days and is cleared if the server restarts; passwords are never saved in the browser.
- Temporary accounts use a unique username only and play with 500 credits. Their account row, credits, and bet are deleted after disconnect (with a 15-second handoff window for switching games) or when the server shuts down. If the host loses power or the process is force-killed, leftover temporary data is removed at the next startup.
- Bets are whole amounts from 1–50,000 credits; regular wins pay 1:1 and blackjack pays 3:2, rounded down to whole credits for odd bets.
- Roulette uses the American wheel with 0, 00, and 1–36. It supports straight-up, split, street, corner, six-line, top-line, dozen, column, red/black, odd/even, and low/high bets. Standard American payouts apply; each game uses the same account credits, and roulette wins return the original stake plus the listed winnings.
- The dealer peeks for blackjack, then draws until reaching 17 if no blackjack is found.
- Choose a decision timer of 5–300 seconds when starting each round (30 seconds by default). The timer resets for each decision and stands the active hand when it expires.
- Double down is allowed on any first two cards, including after a split.
- Matching pairs can be split and resplit as long as the player can match the bet. Split aces can be resplit but cannot be hit.
- A player who disconnects during a hand forfeits that hand's bet.
- Passwords for permanent accounts are stored as hashes. Their credits and preferred bets remain when the game server restarts.
- Active hands and the shoe are kept in memory; a server restart ends an active hand and automatically refunds its unsettled bets at the next startup.

This is a casual game with play credits, not a payment or gambling service. The default server uses plain HTTP, so passwords are not encrypted while traveling over the network. Use it with people you trust, do not reuse an important password, and put it behind HTTPS before making it broadly available. Anyone who can reach the forwarded port can create an account and join.

The database also has `player`, `admin`, and `debug` roles, an account-to-role mapping, permissions, and a `database.has_permission(account_id, permission_key)` helper. The admin credit command is available now; debug tools and in-game role management are not implemented.

The configured admin has an admin tools panel. Enable **Manual draw** to pause each card draw; only the admin sees the shoe's next card. Click **Confirm next draw** or enter `/draw` to take exactly one card. Disable the toggle to resume automatic dealing. Manual mode applies to the initial deal, player hits, doubles, splits, and dealer draws.

Use `/stackcard value[suit]` to move the next matching card to the top of the remaining shoe. Examples: `/stackcard A` finds the next Ace of any suit; `/stackcard 10H` finds only a ten of hearts; `/stackcard JH` finds a Jack of hearts; `/stackcard AS` finds an ace of spades. You can also use `ACE`, `JACK`, `QUEEN`, `KING`, or `TEN`. Search applies to cards remaining in the shoe, not cards already dealt.

Players press **Ready** before each blackjack round; the round starts once every seated player who can afford their bet is ready. Their player card shows a ready indicator, and they can cancel readiness before the round begins. Blackjack admin commands include `/forcestart [seconds]` to start without waiting for player readiness, `/skip`, which forces the player currently deciding to stand and advances play; `/timer seconds`, which sets the active decision's remaining time (1–3,600 seconds); `/timer pause` and `/timer resume`; `/setcredits username amount`, which sets an account's balance exactly (from 0 to 1,000,000,000 credits); `/manualdraw on|off`; `/draw`; and `/stackcard value[suit]`. `/skip` and changing the remaining timer are unavailable while a manual card draw is pending. The server checks the account's `admin.access` permission before applying admin commands.

Roulette also uses **Ready**: click the betting board to place chips, then press Ready. The wheel spins once every seated player with credits or chips down is ready; pressing Ready with no chips down sits out that spin, and changing bets cancels your readiness. Roulette admin commands are `/forceresult number|clear`, which makes the next spin land on that pocket (0, 00, or 1–36) and is only visible to admins, `/forcestart` to spin without waiting for readiness, and the shared `/setcredits username amount`.

Craps uses standard casino rules on a single-sided layout: pass and don't pass (12 on the come-out pushes don't bets), come and don't come, free odds at true odds (3-4-5x; lay odds up to 6x), place bets (9:5, 7:5, 7:6), the field (2 pays double, 12 pays triple), Big 6/8, hardways (7:1 and 9:1), and one-roll bets (any seven 4:1, any craps 7:1, 3 and 11 15:1, 2 and 12 30:1). Place bets, hardways, and come-bet odds are off on the come-out roll. Click the layout to place chips and press **Ready**; once every seated player is ready, the shooter gets a **Roll dice** button and rolls. Bets that stay up between rolls (line and come bets with a point, place, hardways, odds) keep their credits on the table and are refunded if the server stops. Right-click a spot or press × on your seat to remove new chips or take down a bet that may come down; pass and come bets with a point can't come down, and leaving the table loses them while your other bets are returned. The shooter changes after a seven-out. Craps admin commands are `/forceroll die1 die2|clear` to set the next roll, `/forcestart` to roll without waiting for readiness, and the shared `/setcredits username amount`.

Poker is no-limit Texas Hold'em between the seated players, with no house edge: every blind, call, and raise comes straight out of your account credits, and the pots (including side pots for all-in players) are paid back to the winners at the end of the hand. Wagers left in a hand when the server stops are refunded at the next startup, as in the other games. Press **Ready** to be dealt in; a hand starts once at least two players with enough credits for the big blind are all ready, and the dealer button moves each hand. Unlike the other tables, you can sit down during a hand and are dealt in from the next one. If your decision timer runs out you check when you can and fold otherwise, and leaving during a hand folds it. Poker admin commands are `/forcestart [seconds]` to deal without waiting for readiness, `/skip` to check or fold for the deciding player, `/timer seconds|pause|resume`, `/manualdraw on|off` and `/draw` (the same manual-draw mode as blackjack: every hole and board card waits for the admin to confirm it, with the next card previewed), `/blinds amount` to set the big blind between hands (the small blind is half; default 10), `/stackcard rank[suit]` to put a card on top of the deck, and the shared `/setcredits username amount`.

The admin terminal at `/admin` lists every player connected to any game, with their game, account type, credits, and time connected, and refreshes live. Sign in with the admin account, or press **Open admin terminal** in a game's admin panel: it opens over the table, already signed in, and you keep your seat in the game while it's open (it also offers a link to open it in a new tab). Each row has a **Disconnect** button, and the terminal accepts `/players`, `/disconnect username` (disconnects that account from whichever game it is in), `/setcredits username amount`, `/clear`, and `/help`. A disconnected player sees a notice and can reload the page to rejoin.

## Optional settings

Use a different port by setting `PORT` before starting the server (for example, `PORT=8080 python server.py` on macOS/Linux). If you change it, update the router rule and the address you share.

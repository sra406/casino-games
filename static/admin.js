(() => {
  const { socket, $ } = window.RoyalTable;
  // Same per-tab sign-in token the game pages store, so an admin who is
  // already signed in at a table does not have to sign in again.
  const sessionStorageKey = 'royal-table-account-session';
  const history = [];
  let historyIndex = 0;
  let signedIn = false;
  let players = [];
  let clockOffset = 0;

  function readToken() {
    try { return sessionStorage.getItem(sessionStorageKey); } catch { return null; }
  }

  function print(lines, level = 'info') {
    const log = $('admin-terminal-log');
    for (const text of [].concat(lines)) {
      const line = document.createElement('div');
      line.className = `admin-terminal-line ${level}`;
      line.textContent = text;
      log.append(line);
    }
    log.scrollTop = log.scrollHeight;
  }

  function showLogin(text) {
    signedIn = false;
    $('admin-console').hidden = true;
    $('admin-login').hidden = false;
    if (text) $('admin-login-note').textContent = text;
  }

  function runCommand(command) {
    const trimmed = command.trim();
    if (!trimmed) return;
    history.push(trimmed); historyIndex = history.length;
    print(`› ${trimmed}`, 'command');
    if (trimmed.toLowerCase() === '/clear') { $('admin-terminal-log').replaceChildren(); return; }
    socket.emit('admin_console_command', {command: trimmed});
  }

  function formatDuration(seconds) {
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  }

  function renderPlayers() {
    const now = Date.now() / 1000 + clockOffset;
    $('admin-player-count').textContent = players.length;
    $('admin-empty').hidden = players.length > 0;
    $('admin-player-rows').replaceChildren(...players.map((player) => {
      const row = document.createElement('tr');
      const nameCell = document.createElement('td');
      const name = document.createElement('strong'); name.textContent = player.username;
      const type = document.createElement('span'); type.className = `admin-account-type ${player.account_type}`; type.textContent = player.account_type;
      nameCell.append(name, type);
      const game = document.createElement('td');
      const gameTag = document.createElement('span'); gameTag.className = `admin-game-tag ${player.game}`; gameTag.textContent = player.game;
      game.append(gameTag);
      const credits = document.createElement('td'); credits.className = 'numeric';
      credits.textContent = player.credits == null ? '—' : player.credits.toLocaleString();
      const connected = document.createElement('td');
      connected.textContent = player.connected_at ? formatDuration(now - player.connected_at) : '—';
      const actions = document.createElement('td'); actions.className = 'admin-row-actions';
      const kick = document.createElement('button');
      kick.type = 'button'; kick.className = 'button admin-disconnect-button'; kick.textContent = 'Disconnect';
      kick.setAttribute('aria-label', `Disconnect ${player.username}`);
      kick.addEventListener('click', () => {
        if (window.confirm(`Disconnect ${player.username} from ${player.game}?`)) runCommand(`/disconnect ${player.username}`);
      });
      actions.append(kick);
      row.append(nameCell, game, credits, connected, actions);
      return row;
    }));
  }

  socket.on('connect', () => {
    const token = readToken();
    if (token) socket.emit('admin_console_resume', {token});
    else showLogin();
  });
  socket.on('admin_console_ready', (data) => {
    signedIn = true;
    $('admin-login').hidden = true;
    $('admin-console').hidden = false;
    $('admin-terminal-title').textContent = `${data.username}@royal-table`;
    $('admin-password').value = '';
    $('admin-terminal-input').focus();
  });
  socket.on('admin_console_denied', (data) => {
    if (signedIn && data?.text) print(data.text, 'error');
    showLogin(data?.silent ? null : data?.text);
  });
  socket.on('admin_console_output', (data) => print(data.lines || [], data.level || 'info'));
  socket.on('admin_players', (data) => {
    players = data.players || [];
    if (data.server_time) clockOffset = data.server_time - Date.now() / 1000;
    renderPlayers();
  });

  $('admin-login-form').addEventListener('submit', (event) => {
    event.preventDefault();
    socket.emit('admin_console_login', {username: $('admin-username').value.trim(), password: $('admin-password').value});
  });
  $('admin-terminal-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = $('admin-terminal-input');
    runCommand(input.value);
    input.value = '';
  });
  $('admin-terminal-input').addEventListener('keydown', (event) => {
    const input = event.target;
    if (event.key === 'ArrowUp' && history.length) {
      event.preventDefault(); historyIndex = Math.max(0, historyIndex - 1); input.value = history[historyIndex];
    } else if (event.key === 'ArrowDown' && history.length) {
      event.preventDefault(); historyIndex = Math.min(history.length, historyIndex + 1); input.value = history[historyIndex] || '';
    }
  });
  $('admin-terminal-log').addEventListener('click', () => {
    if (!window.getSelection()?.toString()) $('admin-terminal-input').focus();
  });
  $('admin-refresh').addEventListener('click', () => socket.emit('admin_console_refresh'));

  // Credits change during play, so refresh the list periodically while signed in.
  setInterval(() => {
    if (signedIn && socket.connected && !document.hidden) socket.emit('admin_console_refresh');
    else if (signedIn) renderPlayers();
  }, 5000);
})();

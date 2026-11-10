// Shared client for chip-layout tables (roulette, craps): the clickable SVG board
// with chips, the chip rack and bet buttons (templates/partials/chip-controls.html),
// and the player cards listing each seat's bets.
(() => {
  const { socket, $, showNotice } = window.RoyalTable;
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const CHIP_VALUES = [1, 5, 10, 25, 100, 500, 1000, 5000];

  const shortAmount = (value) => {
    if (value >= 1_000_000) return `${+(value / 1_000_000).toFixed(1)}M`;
    if (value >= 10_000) return `${Math.round(value / 1000)}k`;
    if (value >= 1000) return `${+(value / 1000).toFixed(1)}k`;
    return String(value);
  };
  const chipColor = (value) => {
    if (value >= 5000) return 'orange';
    if (value >= 1000) return 'gold';
    if (value >= 500) return 'purple';
    if (value >= 100) return 'black';
    if (value >= 25) return 'green';
    if (value >= 10) return 'blue';
    if (value >= 5) return 'red';
    return 'white';
  };

  function svg(tag, attrs = {}, parent) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    if (parent) parent.append(el);
    return el;
  }

  /* Board: spots are clickable areas (or display-only chip positions) keyed by kind|selection. */
  function createBoard(root, { onHover } = {}) {
    const cellLayer = svg('g', { class: 'board-cells' }, root);
    const spotLayer = svg('g', { class: 'board-spots' }, root);
    const markerLayer = svg('g', { class: 'board-markers', 'pointer-events': 'none' }, root);
    const chipLayer = svg('g', { class: 'board-chips', 'pointer-events': 'none' }, root);
    const spots = new Map();
    const spotKey = (kind, selection) => `${kind}|${selection}`;
    const handlers = { place: () => {}, remove: () => {} };

    // `element` is the drawn cell to make clickable; without one a round hotspot is added.
    // Pass {display: true} for a spot that only positions chips (e.g. come bets moved to a number).
    function addSpot(kind, selection, rect, label, element, extra = {}) {
      const key = spotKey(kind, selection);
      const center = extra.center || [rect.x + rect.w / 2, rect.y + rect.h / 2];
      const spot = { kind, selection, center, label, ...extra };
      spots.set(key, spot);
      if (extra.display) return spot;
      let target = element;
      if (!target) {
        target = svg('rect', { x: rect.x, y: rect.y, width: rect.w, height: rect.h, rx: Math.min(rect.w, rect.h) / 2, class: 'board-hotspot' }, spotLayer);
      }
      target.classList.add('board-spot');
      target.setAttribute('tabindex', '0');
      target.setAttribute('role', 'button');
      target.setAttribute('aria-label', label);
      const title = svg('title', {}, target); title.textContent = label;
      target.dataset.key = key;
      spot.element = target;
      return spot;
    }

    let hovered = null;
    function setHover(key) {
      if (hovered === key) return;
      root.querySelectorAll('.board-spot.hover').forEach((el) => el.classList.remove('hover'));
      hovered = key;
      const spot = key && spots.get(key);
      if (spot?.element) spot.element.classList.add('hover');
      onHover?.(spot || null);
    }
    const spotFrom = (event) => spots.get(event.target.closest('.board-spot')?.dataset.key || '');
    root.addEventListener('pointerover', (e) => setHover(e.target.closest('.board-spot')?.dataset.key || null));
    root.addEventListener('pointerleave', () => setHover(null));
    root.addEventListener('focusin', (e) => setHover(e.target.closest('.board-spot')?.dataset.key || null));
    root.addEventListener('focusout', () => setHover(null));
    root.addEventListener('click', (e) => { const spot = spotFrom(e); if (spot) handlers.place(spot); });
    root.addEventListener('contextmenu', (e) => {
      const spot = spotFrom(e);
      if (!spot) return;
      e.preventDefault();
      handlers.remove(spot);
    });
    root.addEventListener('keydown', (e) => {
      const spot = spotFrom(e);
      if (!spot) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handlers.place(spot); }
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); handlers.remove(spot); }
    });

    function drawChip(x, y, amount, { mine, ghost, won }) {
      const g = svg('g', { class: `board-chip chip-${chipColor(amount)}${mine ? ' mine' : ' other'}${ghost ? ' ghost' : ''}${won ? ' won' : ''}`, transform: `translate(${x} ${y})` }, chipLayer);
      const r = mine ? 14 : 11;
      svg('circle', { r: r + 1.5, class: 'chip-shadow', cy: 1.5 }, g);
      svg('circle', { r, class: 'chip-body' }, g);
      svg('circle', { r: r - 2.2, class: 'chip-edge' }, g);
      svg('circle', { r: r - 5, class: 'chip-inner' }, g);
      const text = svg('text', { class: 'chip-text', 'font-size': mine ? (amount >= 1000 ? 8.5 : 10) : 8 }, g);
      text.textContent = shortAmount(amount);
    }

    function render({ ownBets, otherBets, ghost = false }) {
      chipLayer.replaceChildren();
      const own = new Map();
      const others = new Map();
      for (const bet of ownBets) {
        const key = spotKey(bet.kind, bet.selection);
        const entry = own.get(key) || { amount: 0, won: false };
        entry.amount += bet.amount; entry.won = entry.won || bet.won;
        own.set(key, entry);
      }
      for (const bet of otherBets) {
        const key = spotKey(bet.kind, bet.selection);
        others.set(key, (others.get(key) || 0) + bet.amount);
      }
      for (const [key, amount] of others) {
        const spot = spots.get(key); if (!spot) continue;
        const [x, y] = spot.center;
        drawChip(own.has(key) ? x + 9 : x, own.has(key) ? y - 9 : y, amount, { mine: false, ghost });
      }
      for (const [key, entry] of own) {
        const spot = spots.get(key); if (!spot) continue;
        drawChip(spot.center[0], spot.center[1], entry.amount, { mine: true, ghost, won: ghost && entry.won });
      }
    }

    function setEnabled(enabled) { root.classList.toggle('locked', !enabled); }

    return { root, cellLayer, spotLayer, markerLayer, spots, spotKey, addSpot, handlers, render, setEnabled };
  }

  /* Chip rack, totals and bet buttons, wired to `${prefix}_…` socket events. */
  function setupControls({ prefix, board, getState, isBusy, busyNotice, canRemove = (bet) => bet.staged > 0 }) {
    const ownPlayer = () => { const state = getState(); return state?.players.find((p) => p.sid === state.own_sid); };
    const emit = (event, data) => (data === undefined ? socket.emit(`${prefix}_${event}`) : socket.emit(`${prefix}_${event}`, data));
    const rack = $('chip-rack');
    CHIP_VALUES.forEach((value) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `rack-chip chip-${chipColor(value)}`;
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', 'false');
      button.setAttribute('aria-label', `${value.toLocaleString()} credit chip`);
      button.dataset.value = value;
      button.innerHTML = `<span>${shortAmount(value)}</span>`;
      button.addEventListener('click', () => emit('set_bet', { bet: value }));
      rack.append(button);
    });
    $('set-chip-button').addEventListener('click', () => emit('set_bet', { bet: $('chip-input').value }));
    $('chip-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('set-chip-button').click(); });
    $('undo-bet-button').addEventListener('click', () => emit('undo_bet'));
    $('clear-bets-button').addEventListener('click', () => emit('clear_bets'));
    $('rebet-button').addEventListener('click', () => emit('rebet'));
    $('ready-button').addEventListener('click', () => emit('toggle_ready'));

    const removeBet = (index) => emit('remove_bet', { index });
    board.handlers.place = (spot) => {
      if (!ownPlayer()) { showNotice('Sign in or join temporarily to place bets.'); return; }
      if (isBusy()) { showNotice(busyNotice); return; }
      emit('add_bet', { kind: spot.kind, selection: spot.selection });
    };
    board.handlers.remove = (spot) => {
      const own = ownPlayer();
      if (!own || isBusy()) return;
      const index = own.bets.findIndex((bet) => bet.kind === spot.kind && bet.selection === spot.selection && canRemove(bet));
      if (index >= 0) removeBet(index);
    };

    function render({ busyLabel }) {
      const state = getState();
      const own = ownPlayer();
      const busy = isBusy();
      for (const button of rack.children) {
        const selected = Boolean(own) && Number(button.dataset.value) === own.bet;
        button.classList.toggle('selected', selected);
        button.setAttribute('aria-checked', String(selected));
        button.disabled = !own;
      }
      if (own && document.activeElement !== $('chip-input')) $('chip-input').value = own.bet;
      $('chip-input').disabled = !own;
      $('set-chip-button').disabled = !own;
      $('own-credits').textContent = own ? own.credits.toLocaleString() : '—';
      $('own-total-bet').textContent = own ? own.bets.reduce((sum, bet) => sum + bet.amount, 0).toLocaleString() : '0';
      const staged = own ? own.bets.reduce((sum, bet) => sum + bet.staged, 0) : 0;
      $('undo-bet-button').disabled = !own || busy || !own.can_undo;
      $('clear-bets-button').disabled = !own || busy || !staged;
      $('rebet-button').disabled = !own || busy || staged > 0 || !own.can_rebet;
      const eligible = state.players.filter((p) => p.ready_eligible);
      const readyCount = eligible.filter((p) => p.ready).length;
      const readyButton = $('ready-button');
      readyButton.disabled = !own || busy || !own.ready_eligible;
      readyButton.classList.toggle('is-ready', Boolean(own?.ready));
      readyButton.textContent = busy ? busyLabel
        : own?.ready ? 'Cancel ready'
          : own && !own.bets.length ? 'Ready · sit out' : 'Ready';
      $('ready-count').textContent = busy || !eligible.length ? ''
        : `${readyCount} of ${eligible.length} player${eligible.length === 1 ? '' : 's'} ready`;
    }

    return { render, removeBet, ownPlayer };
  }

  /* Player card listing live bets (with remove buttons) or the last play's results. */
  function playerCard(player, { ownSid, busy, readyBadge, canRemove = (bet) => bet.staged > 0, onRemove }) {
    const mine = player.sid === ownSid;
    const card = document.createElement('article');
    card.className = `player-card chip-player${mine ? ' mine' : ''}`;
    if (player.profile_image_url) { card.classList.add('has-profile-image'); card.style.setProperty('--profile-image', `url("${player.profile_image_url}")`); }
    const top = document.createElement('div'); top.className = 'player-top';
    const name = document.createElement('span'); name.className = 'player-name'; name.textContent = player.name;
    if (player.account_type === 'temporary') { const temp = document.createElement('span'); temp.className = 'account-badge'; temp.textContent = 'TEMP'; name.append(' ', temp); }
    if (player.ready && !busy) {
      const badge = document.createElement('span'); badge.className = 'ready-indicator'; badge.textContent = readyBadge(player); name.append(badge);
    }
    const actions = document.createElement('span'); actions.className = 'player-top-actions';
    if (mine) {
      const label = document.createElement('label'); label.className = 'profile-upload';
      label.innerHTML = '<span>Photo</span><input id="profile-picture-input" type="file" accept="image/jpeg,image/png,image/webp" aria-label="Upload profile picture">';
      actions.append(label);
    }
    const status = document.createElement('span'); status.className = `player-status ${player.status.replace(/\s+/g, '-')}`; status.textContent = player.status;
    actions.append(status);
    top.append(name, actions); card.append(top);

    const meta = document.createElement('div'); meta.className = 'player-meta';
    const balance = document.createElement('span'); balance.textContent = `Available credit ${player.credits.toLocaleString()}`;
    const delta = document.createElement('span');
    delta.className = 'player-credit-change ' + (player.round_credit_change > 0 ? 'positive' : player.round_credit_change < 0 ? 'negative' : 'neutral');
    delta.textContent = player.round_credit_change == null ? '—' : `${player.round_credit_change > 0 ? '+' : ''}${player.round_credit_change.toLocaleString()}`;
    meta.append(balance, delta); card.append(meta);

    const list = document.createElement('div'); list.className = 'chip-player-bets';
    const addLine = (text, extra) => {
      const line = document.createElement('div'); line.className = 'chip-bet-line';
      const label = document.createElement('span'); label.textContent = text; line.append(label);
      if (extra) line.append(extra);
      list.append(line);
      return line;
    };
    for (const result of player.last_bets) {
      const outcome = document.createElement('strong');
      if (result.outcome) {
        outcome.textContent = result.outcome;
        outcome.className = result.won ? 'positive' : /lost/.test(result.outcome) ? 'negative' : '';
      } else {
        outcome.textContent = result.won ? `+${result.payout.toLocaleString()}` : 'Lost';
        outcome.className = result.won ? 'positive' : 'negative';
      }
      addLine(`${result.description} · ${result.amount.toLocaleString()}`, outcome).classList.add('result');
    }
    player.bets.forEach((bet, index) => {
      let extra = null;
      if (mine && !busy && canRemove(bet)) {
        extra = document.createElement('button'); extra.type = 'button'; extra.className = 'chip-remove-bet'; extra.textContent = '×';
        extra.title = bet.staged ? 'Remove new chips' : 'Take down';
        extra.setAttribute('aria-label', `${extra.title}: ${bet.description}`);
        extra.addEventListener('click', () => onRemove(index));
      }
      const pending = bet.staged && bet.locked ? ` (+${bet.staged.toLocaleString()} new)` : bet.staged ? ' · new' : '';
      addLine(`${bet.description} · ${bet.amount.toLocaleString()}${pending}`, extra);
    });
    if (!list.children.length) list.textContent = 'No bets placed';
    card.append(list);
    return card;
  }

  window.RoyalChips = { svg, shortAmount, chipColor, CHIP_VALUES, createBoard, setupControls, playerCard };
})();

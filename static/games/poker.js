// Texas Hold'em table client. The server decides everything; this file renders the
// table and sends the player's actions.
(() => {
  const {
    socket, $, escapeHtml, cardMarkup, formatCredits, formatCreditChange, profileUploadMarkup, profileStyle,
    renderAdminControls,
  } = window.RoyalTable;
  const IN_HAND = ['preflop', 'flop', 'turn', 'river'];
  let state = null;

  socket.on('connect', () => socket.emit('poker_refresh_state'));
  socket.on('disconnect', () => {
    state = null;
    $('join-card').hidden = false;
    $('players').replaceChildren();
    $('turn-controls').hidden = true;
  });
  socket.on('poker_state', (next) => { state = next; render(); });

  function seatBadges(player) {
    const badges = [];
    if (player.is_dealer) badges.push('<span class="poker-badge dealer" title="Dealer button">D</span>');
    if (player.is_small_blind) badges.push('<span class="poker-badge" title="Small blind">SB</span>');
    if (player.is_big_blind) badges.push('<span class="poker-badge" title="Big blind">BB</span>');
    return badges.join('');
  }

  function playerStatus(player) {
    if (!player.connected) return { text: 'offline', cls: 'lost' };
    if (state.phase === 'round_over' && player.in_hand) {
      if (player.won > 0) return { text: `won ${formatCredits(player.won)}`, cls: 'won' };
      return { text: player.status, cls: 'lost' };
    }
    if (player.all_in) return { text: 'all-in', cls: 'blackjack' };
    if (player.folded) return { text: 'folded', cls: 'lost' };
    return { text: player.status, cls: '' };
  }

  function playerCard(player) {
    const mine = player.sid === state.own_sid;
    const deciding = player.sid === state.turn_sid;
    const status = playerStatus(player);
    const best = new Set(player.best_cards);
    const hole = player.hole.length
      ? player.hole.map((card) => cardMarkup(card, false, best.has(card) ? 'best-card' : '')).join('')
      : `<span class="muted">${player.in_hand || IN_HAND.includes(state.phase) ? 'Sitting out' : 'Cards appear here'}</span>`;
    const accountBadge = player.account_type === 'temporary' ? '<span class="account-badge">TEMP</span>' : '';
    const readyBadge = player.ready && !IN_HAND.includes(state.phase) ? '<span class="ready-indicator">READY</span>' : '';
    const change = state.phase === 'round_over' && player.round_credit_change != null
      ? `<span class="player-credit-change ${player.round_credit_change > 0 ? 'positive' : player.round_credit_change < 0 ? 'negative' : 'neutral'}">${formatCreditChange(player.round_credit_change)}</span>`
      : '';
    const streetBet = player.street_bet > 0 && IN_HAND.includes(state.phase)
      ? `<span class="poker-street-bet" title="Bet this round">${formatCredits(player.street_bet)}</span>` : '';
    const action = player.last_action && IN_HAND.includes(state.phase)
      ? `<span class="poker-last-action">${escapeHtml(player.last_action)}</span>` : '';
    const handLabel = player.hand_label ? `<span class="poker-hand-label">${escapeHtml(player.hand_label)}</span>` : '';
    return `<article class="player-card poker-seat ${mine ? 'mine' : ''} ${deciding ? 'active' : ''} ${player.folded ? 'folded' : ''} ${player.won > 0 && state.phase === 'round_over' ? 'winner' : ''} ${player.profile_image_url ? 'has-profile-image' : ''}" ${profileStyle(player)}>
      <div class="player-top"><span class="player-name">${seatBadges(player)}${escapeHtml(player.name)}${mine ? ' · You' : ''} ${accountBadge} ${readyBadge}</span><span class="player-top-actions">${mine ? profileUploadMarkup(player) : ''}<span class="player-status ${status.cls}">${escapeHtml(status.text)}</span></span></div>
      <div class="player-meta"><span class="player-credit-summary"><span class="player-credit-count">${formatCredits(player.credits)} credits</span>${change}</span>${deciding ? '<span class="decision-indicator">DECIDING</span>' : ''}</div>
      <div class="player-cards poker-hole">${hole}${streetBet}</div>
      <div class="poker-seat-footer">${action}${handLabel}</div>
    </article>`;
  }

  function raiseTarget(kind) {
    const options = state.options;
    if (kind === 'min') return options.min_raise_to;
    const me = state.players.find((player) => player.sid === state.own_sid);
    const potAfterCall = state.pot + options.to_call;
    const portion = kind === 'half' ? Math.floor(potAfterCall / 2) : potAfterCall;
    const target = state.current_bet === 0 ? portion : state.current_bet + portion;
    return Math.max(options.min_raise_to, Math.min(options.max_raise_to, target || (me ? me.street_bet : 0)));
  }

  function renderDecision(me) {
    const options = state.options || {};
    $('turn-controls').hidden = !options.can_act;
    if (!options.can_act) return;
    $('to-call-label').textContent = options.can_check ? 'nothing to call' : `${formatCredits(options.to_call)} to call`;
    const allInCall = !options.can_check && options.to_call >= me.credits;
    $('check-call-button').textContent = options.can_check ? 'Check' : allInCall ? `Call all-in ${formatCredits(options.to_call)}` : `Call ${formatCredits(options.to_call)}`;
    $('allin-button').hidden = allInCall || (!options.can_raise && options.can_check);
    $('allin-button').textContent = options.can_raise ? `All-in ${formatCredits(options.max_raise_to)}` : 'All-in';
    $('raise-controls').hidden = !options.can_raise;
    if (!options.can_raise) return;
    const slider = $('raise-slider');
    const amount = $('raise-amount');
    slider.min = amount.min = options.min_raise_to;
    slider.max = amount.max = options.max_raise_to;
    const current = Number(amount.value);
    if (document.activeElement !== amount && document.activeElement !== slider
        && !(current >= options.min_raise_to && current <= options.max_raise_to && amount.dataset.turn === turnKey())) {
      amount.value = slider.value = options.min_raise_to;
    }
    amount.dataset.turn = turnKey();
    $('raise-button').textContent = `${options.raise_label} to ${formatCredits(Number(amount.value) || options.min_raise_to)}`;
  }

  function turnKey() {
    return `${state.round_number}:${state.street}:${state.current_bet}`;
  }

  function render() {
    if (!state) return;
    const inHand = IN_HAND.includes(state.phase);
    const me = state.players.find((player) => player.sid === state.own_sid);
    $('round-number').textContent = state.round_number || '—';
    $('table-message').textContent = state.message;
    $('player-count').textContent = state.players.length;
    $('blinds').textContent = `${formatCredits(state.small_blind)} / ${formatCredits(state.big_blind)}`;
    $('street').textContent = inHand ? `${state.street}${state.dealing ? ' · dealing' : ''}` : state.phase === 'round_over' ? 'Hand complete' : 'Waiting for players';
    const slots = [...state.board.map((card) => cardMarkup(card, true)),
      ...Array.from({ length: 5 - state.board.length }, () => '<div class="card poker-card-slot"></div>')];
    $('board').innerHTML = slots.join('');
    $('pot').textContent = formatCredits(state.pot);
    $('pots').hidden = !state.pots.length;
    $('pots').innerHTML = state.pots.map((pot) => `<li><span>${escapeHtml(pot.name)} · ${formatCredits(pot.amount)}</span><strong>${pot.winners.map(escapeHtml).join(' & ')}</strong>${pot.hand ? `<em>${escapeHtml(pot.hand)}</em>` : ''}</li>`).join('');

    const timerActive = inHand && state.timer_remaining != null;
    $('decision-timer').hidden = !timerActive;
    if (timerActive) {
      const minutes = Math.floor(state.timer_remaining / 60);
      const seconds = String(state.timer_remaining % 60).padStart(2, '0');
      $('decision-timer').textContent = `${state.timer_paused ? 'Paused · ' : ''}${minutes}:${seconds}`;
    }
    if (document.activeElement !== $('decision-seconds')) $('decision-seconds').value = state.round_decision_seconds;

    $('join-card').hidden = Boolean(me);
    $('admin-card').hidden = !me || !state.own_is_admin;
    if (me && state.own_is_admin) renderAdminControls(state);

    const eligible = state.players.filter((player) => player.ready_eligible);
    const readyCount = eligible.filter((player) => player.ready).length;
    const readyButton = $('ready-button');
    readyButton.textContent = inHand ? 'Hand in progress' : me?.ready ? 'Cancel ready' : 'Ready';
    readyButton.classList.toggle('is-ready', Boolean(me?.ready));
    readyButton.disabled = !me || inHand || !me.ready_eligible;
    readyButton.title = me && !me.ready_eligible ? `You need ${formatCredits(state.big_blind)} credits for the big blind` : '';
    $('decision-seconds').disabled = !me || inHand;
    $('ready-count').textContent = inHand || !eligible.length ? ''
      : eligible.length < 2 ? 'Waiting for a second player'
        : `${readyCount} of ${eligible.length} players ready`;

    if (me) renderDecision(me);
    else $('turn-controls').hidden = true;

    const preserved = document.activeElement?.id === 'profile-picture-input';
    if (!preserved) $('players').innerHTML = state.players.map(playerCard).join('');
  }

  const act = (action, extra = {}) => socket.emit('poker_action', { action, ...extra });
  $('ready-button').addEventListener('click', () => socket.emit('poker_toggle_ready', { decision_seconds: $('decision-seconds').value }));
  $('fold-button').addEventListener('click', () => act('fold'));
  $('check-call-button').addEventListener('click', () => act(state?.options?.can_check ? 'check' : 'call'));
  $('allin-button').addEventListener('click', () => act('allin'));
  $('raise-button').addEventListener('click', () => act('raise', { amount: $('raise-amount').value }));
  $('raise-amount').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); $('raise-button').click(); }
  });
  const syncRaise = (value) => {
    $('raise-amount').value = $('raise-slider').value = value;
    if (state?.options?.can_raise) $('raise-button').textContent = `${state.options.raise_label} to ${formatCredits(Number(value) || 0)}`;
  };
  $('raise-slider').addEventListener('input', (event) => syncRaise(event.target.value));
  $('raise-amount').addEventListener('input', (event) => syncRaise(event.target.value));
  document.querySelectorAll('[data-raise]').forEach((button) => {
    button.addEventListener('click', () => { if (state?.options?.can_raise) syncRaise(raiseTarget(button.dataset.raise)); });
  });
})();

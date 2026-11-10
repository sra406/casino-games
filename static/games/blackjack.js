(() => {
  const {
    socket, $, escapeHtml, cardMarkup, formatCredits, formatCreditChange, profileUploadMarkup, profileStyle,
    renderAdminControls,
  } = window.RoyalTable;
  let state = null;

  socket.on('disconnect', () => {
    state = null;
    $('join-card').hidden = false;
    $('players').replaceChildren();
    $('turn-controls').hidden = true;
  });
  socket.on('state', (next) => { state = next; render(); });

  function isSoftHand(cards) {
    let total = 0;
    let aces = 0;
    for (const card of cards) {
      const rank = card.slice(0, -1);
      if (rank === 'A') {
        total += 1;
        aces += 1;
      } else if (['J', 'Q', 'K'].includes(rank)) {
        total += 10;
      } else {
        total += Number(rank);
      }
    }
    return aces > 0 && total + 10 <= 21;
  }

  function render() {
    if (!state) return;
    $('round-number').textContent = state.round_number || '—';
    $('table-message').textContent = state.message;
    $('player-count').textContent = state.players.length;
    $('dealer-cards').innerHTML = state.dealer_hand.length
      ? state.dealer_hand.map((card) => cardMarkup(card, true)).join('')
      : '<span class="muted">Waiting for a round</span>';
    const dealerBust = state.dealer_total != null && state.dealer_total > 21;
    $('dealer-total').classList.toggle('is-bust', dealerBust);
    $('dealer-total-value').textContent = dealerBust ? 'BUST!' : state.dealer_total == null ? '—' : state.dealer_total;
    const timerActive = state.phase === 'playing' && state.timer_remaining != null;
    $('decision-timer').hidden = !timerActive;
    if (timerActive) {
      const minutes = Math.floor(state.timer_remaining / 60);
      const seconds = String(state.timer_remaining % 60).padStart(2, '0');
      $('decision-timer').textContent = `${state.timer_paused ? 'Paused · ' : ''}${minutes}:${seconds}`;
    }
    if (document.activeElement !== $('decision-seconds')) {
      $('decision-seconds').value = state.round_decision_seconds || 30;
    }

    const me = state.players.find((player) => player.sid === state.own_sid);
    $('join-card').hidden = Boolean(me);
    $('admin-card').hidden = !me || !state.own_is_admin;
    if (me && state.own_is_admin) renderAdminControls(state);
    const roundInProgress = ['playing', 'dealer', 'dealing'].includes(state.phase);
    const readyEligible = state.players.filter((player) => player.connected && player.credits >= player.bet);
    const readyCount = readyEligible.filter((player) => player.ready).length;
    $('start-button').textContent = me?.ready ? 'Cancel ready' : 'Ready';
    $('start-button').disabled = !me || roundInProgress || me.credits < me.bet || readyEligible.length === 0;
    $('start-button').title = readyEligible.length
      ? `${readyCount} of ${readyEligible.length} active players ready`
      : 'No players can afford their current bet';
    $('decision-seconds').disabled = !me || roundInProgress;
    if (me) {
      $('turn-controls').hidden = !(state.phase === 'playing' && state.turn_sid === me.sid);
      $('hit-button').disabled = !state.own_can_hit;
      $('double-button').disabled = !state.own_can_double;
      $('split-button').disabled = !state.own_can_split;
    }

    const oldBetInput = $('bet-amount');
    const preserveBetInput = oldBetInput && document.activeElement === oldBetInput;
    const oldBetValue = preserveBetInput ? oldBetInput.value : null;
    $('players').innerHTML = state.players.map((player) => {
      const mine = player.sid === state.own_sid;
      const active = state.phase === 'playing' && state.turn_sid === player.sid;
      let status = player.connected ? player.status : 'offline';
      if (status === 'waiting') status = 'ready';
      if (status === 'round_over') status = 'done';
      const statusClass = /win|blackjack|stood/.test(status) ? 'won' : /bust|lost|disconnected/.test(status) ? 'lost' : '';
      const accountBadge = player.account_type === 'temporary' ? '<span class="account-badge">TEMP</span>' : '';
      const hands = player.hands.length ? player.hands.map((hand, index) => {
        const activeHand = state.phase === 'playing'
          && player.sid === state.turn_sid
          && player.active_hand_index === index
          && hand.status === 'playing';
        const outcomeClass = state.phase === 'round_over' && hand.outcome ? `hand-result-${hand.outcome}` : '';
        const cards = hand.cards.map((card) => cardMarkup(card)).join('');
        const handStatus = hand.result || hand.status;
        const handStatusClass = hand.outcome === 'loss' ? 'lost'
          : hand.outcome === 'blackjack' ? 'blackjack'
            : hand.outcome === 'push' ? 'push'
              : hand.outcome === 'win' || /win/.test(handStatus) ? 'won'
                : /bust|lost|dealer/.test(handStatus) ? 'lost' : '';
        const creditChange = state.phase === 'round_over' && hand.credit_change != null
          ? `<span class="hand-credit-alert ${outcomeClass}">${hand.credit_change > 0 ? `+${formatCredits(hand.credit_change)} credits won` : hand.credit_change < 0 ? `−${formatCredits(hand.credit_change)} credits` : 'No net change'}</span>`
          : '';
        return `<div class="player-hand ${activeHand ? 'active-decision-hand' : ''} ${outcomeClass}">
          <div class="player-hand-heading"><span>Hand ${index + 1} · Bet ${formatCredits(hand.bet)}</span><span class="hand-heading-detail">${activeHand ? '<span class="decision-indicator">DECIDING</span>' : ''}${creditChange}<span class="player-status ${handStatusClass}">${escapeHtml(handStatus)}</span></span></div>
          <div class="player-cards">${cards}<span class="player-total">${isSoftHand(hand.cards) ? 'Soft ' : ''}${hand.total}</span></div>
        </div>`;
      }).join('') : '<div class="player-cards"><span class="muted">Cards appear here</span></div>';
      const roundCreditChange = state.phase === 'round_over' && player.round_credit_change != null
        ? `<span class="player-credit-change ${player.round_credit_change > 0 ? 'positive' : player.round_credit_change < 0 ? 'negative' : 'neutral'}">${formatCreditChange(player.round_credit_change)}</span>`
        : '';
      const balance = mine
        ? `<div class="player-wallet-group"><span class="wallet-label">Available credits</span><div class="wallet-line player-wallet"><strong>${formatCredits(player.credits)}</strong>${roundCreditChange}</div></div>`
        : `<span class="player-credit-summary"><span class="player-credit-count">${formatCredits(player.credits)} credits</span>${roundCreditChange}</span>`;
      const seatControls = mine
        ? `<div class="own-player-meta"><div class="next-bet-control"><label for="bet-amount">Bet next round</label><div class="input-row"><input id="bet-amount" type="number" min="${state.min_bet}" max="${Math.min(state.max_bet, player.credits)}" step="1" value="${player.bet}"><button id="bet-button" class="button button-secondary" type="button">Set</button></div></div>${balance}</div>`
        : `<div class="player-meta"><span>${player.hands.length} hand${player.hands.length === 1 ? '' : 's'}</span>${balance}</div>`;
      const profileControl = mine ? profileUploadMarkup(player) : '';
      const readyBadge = player.ready ? '<span class="ready-indicator">READY</span>' : '';
      return `<article class="player-card ${mine ? 'mine' : ''} ${active ? 'active' : ''} ${player.profile_image_url ? 'has-profile-image' : ''}" ${profileStyle(player)}>
        <div class="player-top"><span class="player-name">${escapeHtml(player.name)}${mine ? ' · You' : ''} ${accountBadge} ${readyBadge}</span><span class="player-top-actions">${profileControl}<span class="player-status ${statusClass}">${escapeHtml(status)}</span></span></div>
        ${seatControls}
        ${hands}</article>`;
    }).join('');
    if (preserveBetInput) {
      const betInput = $('bet-amount');
      betInput.value = oldBetValue;
      betInput.focus({ preventScroll: true });
    }
  }

  $('start-button').addEventListener('click', () => socket.emit('toggle_ready', {
    decision_seconds: $('decision-seconds').value,
  }));
  $('players').addEventListener('click', (event) => {
    if (event.target.closest('#bet-button')) {
      socket.emit('set_bet', { bet: $('bet-amount').value });
    }
  });
  $('players').addEventListener('keydown', (event) => {
    if (event.target.id === 'bet-amount' && event.key === 'Enter') {
      event.preventDefault();
      $('bet-button').click();
    }
  });
  $('hit-button').addEventListener('click', () => socket.emit('hit'));
  $('double-button').addEventListener('click', () => socket.emit('double_down'));
  $('split-button').addEventListener('click', () => socket.emit('split'));
  $('stand-button').addEventListener('click', () => socket.emit('stand'));
})();

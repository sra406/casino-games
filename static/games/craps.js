// Craps table client: a single-sided casino layout drawn in SVG, the dice, and the
// shared chip controls. The server rolls and settles; this file only renders.
(() => {
  const { socket, $ } = window.RoyalTable;
  const { svg, createBoard, setupControls, playerCard } = window.RoyalChips;
  const POINTS = [4, 5, 6, 8, 9, 10];
  const FIELD = [2, 3, 4, 9, 10, 11, 12];
  const PIPS = { 1: [5], 2: [1, 9], 3: [1, 5, 9], 4: [1, 3, 7, 9], 5: [1, 3, 5, 7, 9], 6: [1, 3, 4, 6, 7, 9] };
  let state = null;

  const label = (parent, x, y, text, cls = 'craps-label', extra = {}) => {
    const t = svg('text', { x, y, class: cls, ...extra }, parent);
    t.textContent = text;
    return t;
  };

  function drawDieFace(parent, x, y, size, value) {
    svg('rect', { x, y, width: size, height: size, rx: size * 0.18, class: 'craps-die-face' }, parent);
    const step = size / 4;
    PIPS[value].forEach((cell) => {
      const col = (cell - 1) % 3, row = Math.floor((cell - 1) / 3);
      svg('circle', { cx: x + step * (col + 1), cy: y + step * (row + 1), r: size * 0.085, class: 'craps-die-pip' }, parent);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Layout                                                              */
  /* ------------------------------------------------------------------ */
  const board = (() => {
    const shared = createBoard($('craps-board'));
    const { cellLayer, markerLayer } = shared;
    const cells = new Map(); // key -> drawn group, for winner highlights

    function cell(key, rect, kind, selection, text, sub, spotLabel, extra = {}) {
      const g = svg('g', { class: `board-cell craps-cell ${extra.cls || ''}` }, cellLayer);
      svg('rect', { x: rect.x, y: rect.y, width: rect.w, height: rect.h, rx: extra.rx ?? 6, class: 'cell-bg' }, g);
      if (text) label(g, rect.x + rect.w / 2, rect.y + rect.h / 2 + (sub ? -7 : 0), text, extra.textCls || 'craps-label');
      if (sub) label(g, rect.x + rect.w / 2, rect.y + rect.h / 2 + 13, sub, 'craps-sublabel');
      cells.set(key, g);
      if (kind) shared.addSpot(kind, selection, rect, spotLabel, g, extra.spot || {});
      return g;
    }

    // Number boxes with an odds strip on top for come / don't come bets that moved there.
    const NX = 85, NW = (695 - NX) / 6, NY = 10, STRIP = 28, NH = 160;
    POINTS.forEach((n, i) => {
      const x = NX + i * NW;
      const name = { 6: 'SIX', 9: 'NINE' }[n] || String(n);
      const pays = { 4: '9 to 5', 10: '9 to 5', 5: '7 to 5', 9: '7 to 5', 6: '7 to 6', 8: '7 to 6' }[n];
      cell(`odds|${n}`, { x, y: NY, w: NW, h: STRIP }, 'odds', String(n), 'ODDS', '', `Odds on your come or don't come bet on ${n}`, { textCls: 'craps-strip-label', rx: 4, cls: 'craps-strip' });
      const box = cell(`place|${n}`, { x, y: NY + STRIP, w: NW, h: NH - STRIP }, 'place', String(n), '', '', `Place ${n} · ${pays}`, { spot: { center: [x + NW / 2, NY + STRIP + 82] } });
      label(box, x + NW / 2, NY + STRIP + 34, name, 'craps-number');
      label(box, x + NW / 2, NY + STRIP + 56, `place ${pays}`, 'craps-sublabel');
      // Display-only chip positions for bets the dealer moves onto the number.
      const lowY = NY + NH - 18;
      shared.addSpot('come', String(n), { x, y: lowY, w: 0, h: 0 }, `Come ${n}`, null, { display: true, center: [x + 20, lowY] });
      shared.addSpot('come_odds', String(n), { x, y: lowY, w: 0, h: 0 }, `Come odds ${n}`, null, { display: true, center: [x + 46, lowY] });
      shared.addSpot('dontcome', String(n), { x, y: NY + STRIP + 16, w: 0, h: 0 }, `Don't come ${n}`, null, { display: true, center: [x + NW - 18, NY + STRIP + 16] });
      shared.addSpot('dontcome_odds', String(n), { x, y: NY + STRIP + 16, w: 0, h: 0 }, `Don't come odds ${n}`, null, { display: true, center: [x + 18, NY + STRIP + 16] });
    });

    const dc = cell('dontcome|', { x: 10, y: NY, w: 70, h: NH }, 'dontcome', '', '', '', "Don't come · 1 to 1 (bar 12)", { spot: { center: [45, NY + 120] } });
    label(dc, 45, NY + 52, "DON'T", 'craps-label small');
    label(dc, 45, NY + 70, 'COME', 'craps-label small');
    label(dc, 45, NY + 88, 'BAR 12', 'craps-sublabel');

    cell('come|', { x: NX, y: 175, w: 695 - NX, h: 58 }, 'come', '', 'COME', '', 'Come · 1 to 1', { textCls: 'craps-label big' });
    const field = cell('field|', { x: NX, y: 238, w: 695 - NX, h: 66 }, 'field', '', '', '', 'Field · 2 pays 2:1, 12 pays 3:1, others 1:1', { cls: 'craps-field' });
    FIELD.forEach((n, i) => {
      const x = NX + 50 + i * ((695 - NX - 100) / 6);
      const t = label(field, x, 262, String(n), `craps-field-number${n === 2 || n === 12 ? ' doubled' : ''}`);
      if (n === 2 || n === 12) {
        svg('circle', { cx: x, cy: 257, r: 14, class: 'craps-field-ring' }, field).after(t);
      }
    });
    label(field, NX + (695 - NX) / 2, 292, 'FIELD · 2 pays double · 12 pays triple', 'craps-sublabel');

    cell('big|6', { x: 10, y: 175, w: 70, h: 63 }, 'big', '6', 'BIG 6', '1 to 1', 'Big 6 · 1 to 1', { cls: 'craps-big' });
    cell('big|8', { x: 10, y: 241, w: 70, h: 63 }, 'big', '8', 'BIG 8', '1 to 1', 'Big 8 · 1 to 1', { cls: 'craps-big' });
    cell('dontpass|', { x: 10, y: 309, w: 550, h: 50 }, 'dontpass', '', "DON'T PASS BAR", '12 pushes', "Don't pass · 1 to 1 (bar 12)");
    cell('dontpass_odds|', { x: 565, y: 309, w: 130, h: 50 }, 'dontpass_odds', '', 'LAY ODDS', 'behind don\'t pass', "Lay odds on don't pass · true odds", { cls: 'craps-odds-cell' });
    cell('pass|', { x: 10, y: 364, w: 550, h: 56 }, 'pass', '', 'PASS LINE', '', 'Pass line · 1 to 1', { textCls: 'craps-label big' });
    cell('pass_odds|', { x: 565, y: 364, w: 130, h: 56 }, 'pass_odds', '', 'PASS ODDS', 'true odds', 'Odds behind the pass line · true odds', { cls: 'craps-odds-cell' });

    // Proposition box.
    const PX = 710, PW = 280;
    cell('any7|', { x: PX, y: NY, w: PW, h: 44 }, 'any7', '', 'ANY SEVEN', '4 to 1', 'Any seven · 4 to 1 (one roll)', { cls: 'craps-prop red' });
    [[6, 9], [10, 7], [8, 9], [4, 7]].forEach(([n, pays], i) => {
      const rect = { x: PX + (i % 2) * (PW / 2 + 2), y: 60 + Math.floor(i / 2) * 72, w: PW / 2 - 2, h: 68 };
      const g = cell(`hard|${n}`, rect, 'hard', String(n), '', '', `Hard ${n} · ${pays} to 1`, { cls: 'craps-prop' });
      drawDieFace(g, rect.x + rect.w / 2 - 29, rect.y + 8, 26, n / 2);
      drawDieFace(g, rect.x + rect.w / 2 + 3, rect.y + 8, 26, n / 2);
      label(g, rect.x + rect.w / 2, rect.y + 52, `HARD ${n} · ${pays} to 1`, 'craps-sublabel strong');
    });
    [['two', [1, 1], 30], ['three', [1, 2], 15], ['yo', [5, 6], 15], ['twelve', [6, 6], 30]].forEach(([kind, dice, pays], i) => {
      const rect = { x: PX + i * (PW / 4) + 1, y: 206, w: PW / 4 - 2, h: 74 };
      const g = cell(`${kind}|`, rect, kind, '', '', '', `${dice[0] + dice[1]} · ${pays} to 1 (one roll)`, { cls: 'craps-prop' });
      drawDieFace(g, rect.x + rect.w / 2 - 25, rect.y + 10, 22, dice[0]);
      drawDieFace(g, rect.x + rect.w / 2 + 3, rect.y + 10, 22, dice[1]);
      label(g, rect.x + rect.w / 2, rect.y + 58, `${pays} to 1`, 'craps-sublabel strong');
    });
    cell('anycraps|', { x: PX, y: 286, w: PW, h: 44 }, 'anycraps', '', 'ANY CRAPS', '7 to 1', 'Any craps (2, 3, 12) · 7 to 1 (one roll)', { cls: 'craps-prop red' });
    label(cellLayer, PX + PW / 2, 360, 'ONE-ROLL & HARDWAY BETS', 'craps-sublabel');
    label(cellLayer, PX + PW / 2, 378, 'Hardways and place bets are off on the come-out', 'craps-sublabel');

    function render({ ownBets, otherBets, point, lastRoll, showResult }) {
      shared.render({ ownBets, otherBets });
      markerLayer.replaceChildren();
      for (const g of cells.values()) g.classList.remove('winner');
      const puckAt = point ? [NX + POINTS.indexOf(point) * NW + NW - 16, NY + STRIP + 16] : [45, NY + 22];
      const puck = svg('g', { class: `craps-board-puck ${point ? 'on' : 'off'}`, transform: `translate(${puckAt[0]} ${puckAt[1]})` }, markerLayer);
      svg('circle', { r: 14 }, puck);
      label(puck, 0, 1, point ? 'ON' : 'OFF', 'craps-puck-text');
      if (!showResult || !lastRoll) return;
      const total = lastRoll.total;
      const winners = [];
      if (POINTS.includes(total)) winners.push(`place|${total}`);
      if (FIELD.includes(total)) winners.push('field|');
      if (lastRoll.hard && [4, 6, 8, 10].includes(total)) winners.push(`hard|${total}`);
      if (total === 7) winners.push('any7|');
      if ([2, 3, 12].includes(total)) winners.push('anycraps|');
      if (total === 6 || total === 8) winners.push(`big|${total}`);
      winners.push({ 2: 'two|', 3: 'three|', 11: 'yo|', 12: 'twelve|' }[total]);
      winners.forEach((key) => cells.get(key)?.classList.add('winner'));
    }

    return { ...shared, render };
  })();

  /* ------------------------------------------------------------------ */
  /* Dice                                                                */
  /* ------------------------------------------------------------------ */
  const dice = (() => {
    const els = [$('die-1'), $('die-2')];
    let timer = null;
    let stopAt = 0;
    let rollingId = null;
    let shownId = null;
    const show = (el, value) => {
      el.dataset.value = value;
      el.replaceChildren(...Array.from({ length: 9 }, (_, i) => {
        const pip = document.createElement('span');
        if (PIPS[value].includes(i + 1)) pip.className = 'on';
        return pip;
      }));
    };
    els.forEach((el, i) => show(el, [5, 2][i]));
    function roll(id, values, remainingMs, done) {
      if (rollingId === id || shownId === id) return;
      rollingId = id;
      stopAt = Date.now() + Math.max(250, remainingMs);
      els.forEach((el) => el.classList.add('tumbling'));
      clearInterval(timer);
      timer = setInterval(() => {
        if (Date.now() >= stopAt) {
          clearInterval(timer);
          els.forEach((el, i) => { el.classList.remove('tumbling'); show(el, values[i]); });
          rollingId = null;
          shownId = id;
          done();
          return;
        }
        els.forEach((el) => show(el, 1 + Math.floor(Math.random() * 6)));
      }, 90);
    }
    function rest(values, id) {
      if (rollingId) return;
      shownId = id;
      els.forEach((el, i) => show(el, values[i]));
    }
    return { roll, rest, isAnimating: () => rollingId !== null };
  })();

  /* ------------------------------------------------------------------ */
  /* Controls and rendering                                              */
  /* ------------------------------------------------------------------ */
  const isRolling = () => state?.phase === 'rolling' || dice.isAnimating();
  const canRemove = (bet) => bet.staged > 0 || !['pass', 'come'].includes(bet.kind);
  const controls = setupControls({
    prefix: 'craps', board, getState: () => state, isBusy: isRolling, canRemove,
    busyNotice: 'No more bets — the dice are rolling.',
  });

  function render() {
    if (!state) return;
    const own = state.players.find((p) => p.sid === state.own_sid);
    const rolling = isRolling();
    $('round-number').textContent = state.round_number || '—';
    $('player-count').textContent = state.players.length;
    $('table-message').textContent = rolling ? 'Dice are out…' : state.message;
    $('join-card').hidden = Boolean(own);
    $('admin-card').hidden = !state.own_is_admin;
    $('point-puck').className = `craps-puck ${state.point ? 'on' : 'off'}`;
    $('point-puck-label').textContent = state.point ? 'ON' : 'OFF';
    $('point-label').textContent = state.point ? `Point is ${state.point}` : 'Come-out roll';
    $('shooter-label').textContent = `Shooter: ${state.shooter || '—'}`;
    const history = rolling && state.phase !== 'rolling' ? state.history.slice(1) : state.history;
    $('craps-history').replaceChildren(...history.map((entry, i) => {
      const pip = document.createElement('span');
      pip.className = `history-pip ${entry.total === 7 ? 'red' : [2, 3, 12].includes(entry.total) ? 'black' : 'green'}${i === 0 && !rolling ? ' latest' : ''}`;
      pip.textContent = entry.total;
      pip.title = `${entry.dice[0]}-${entry.dice[1]}`;
      return pip;
    }));
    if (!rolling && state.last_roll) {
      dice.rest(state.last_roll.dice, state.last_roll.roll_number);
      const { dice: d, total } = state.last_roll;
      $('roll-callout').textContent = `${d[0]} + ${d[1]} = ${total}${d[0] === d[1] && [4, 6, 8, 10].includes(total) ? ' (hard)' : ''}`;
    } else if (rolling) {
      $('roll-callout').textContent = 'Rolling…';
    }
    $('admin-forced-roll').textContent = state.forced_next_roll
      ? `Next roll forced: ${state.forced_next_roll[0]}-${state.forced_next_roll[1]} (${state.forced_next_roll[0] + state.forced_next_roll[1]})`
      : 'Next roll: random';
    $('admin-forced-roll').classList.toggle('active', Boolean(state.forced_next_roll));

    // Once everyone is ready the shooter gets the Roll dice button; others see who they wait on.
    const shooterTurn = state.ready_to_roll && !rolling;
    const isShooter = state.shooter_sid === state.own_sid;
    $('roll-button').hidden = !(shooterTurn && isShooter);
    $('roll-button').disabled = false;
    $('shooter-wait').hidden = !(shooterTurn && !isShooter);
    $('shooter-wait').textContent = shooterTurn ? `Everyone is ready · waiting for ${state.shooter} to roll` : '';
    controls.render({ busyLabel: 'Dice are out…' });
    board.render({
      ownBets: own ? own.bets : [],
      otherBets: state.players.filter((p) => p.sid !== state.own_sid).flatMap((p) => p.bets),
      point: state.point,
      lastRoll: state.last_roll,
      showResult: !rolling,
    });
    board.setEnabled(Boolean(own) && !rolling);
    $('players').replaceChildren(...state.players.map((player) => playerCard(player, {
      ownSid: state.own_sid, busy: rolling, canRemove, onRemove: controls.removeBet,
      readyBadge: (p) => (p.bets.length ? 'READY' : 'SITTING OUT'),
    })));
  }

  $('roll-button').addEventListener('click', () => {
    $('roll-button').disabled = true;
    socket.emit('craps_roll');
  });

  socket.on('connect', () => socket.emit('craps_refresh_state'));
  socket.on('craps_state', (next) => {
    state = next;
    if (next.phase === 'rolling' && next.roll) {
      dice.roll(next.roll.id, next.roll.dice, next.roll.remaining_ms, render);
    }
    render();
  });
})();

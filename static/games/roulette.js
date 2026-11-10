(() => {
  const { socket, $ } = window.RoyalTable;
  const { svg, createBoard, setupControls, playerCard } = window.RoyalChips;

  const WHEEL_ORDER = [
    '0', '28', '9', '26', '30', '11', '7', '20', '32', '17', '5', '22',
    '34', '15', '3', '24', '36', '13', '1', '00', '27', '10', '25', '29',
    '12', '8', '19', '31', '18', '6', '21', '33', '16', '4', '23', '35', '14', '2',
  ];
  const RED = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36].map(String));
  const ALL_NUMBERS = Array.from({length: 36}, (_, i) => String(i + 1));

  let state = null;
  let calloutTimer;

  const colorOf = (n) => (n === '0' || n === '00') ? 'green' : RED.has(n) ? 'red' : 'black';
  const numberKey = (n) => n === '0' ? -2 : n === '00' ? -1 : Number(n);
  const sortNumbers = (list) => [...list].sort((a, b) => numberKey(a) - numberKey(b));
  /* ------------------------------------------------------------------ */
  /* Wheel                                                               */
  /* ------------------------------------------------------------------ */
  const wheel = (() => {
    const root = $('roulette-wheel');
    const STEP = 360 / WHEEL_ORDER.length;
    const R_TRACK = 166;
    const R_REST = 119;
    const polar = (r, deg) => {
      const rad = deg * Math.PI / 180;
      return [r * Math.sin(rad), -r * Math.cos(rad)];
    };

    const defs = svg('defs', {}, root);
    const grad = (id, type, stops, attrs = {}) => {
      const g = svg(type, {id, ...attrs}, defs);
      stops.forEach(([offset, color]) => svg('stop', {offset, 'stop-color': color}, g));
    };
    grad('rw-wood', 'radialGradient', [['0.82', '#5a3214'], ['0.93', '#7a461d'], ['1', '#2c160a']]);
    grad('rw-track', 'radialGradient', [['0.80', '#120d08'], ['0.92', '#2a2015'], ['1', '#0b0805']]);
    grad('rw-cone', 'radialGradient', [['0', '#c9a24c'], ['0.18', '#8a5a26'], ['0.55', '#5b3415'], ['1', '#3a200c']]);
    grad('rw-gold', 'linearGradient', [['0', '#fff1b8'], ['0.5', '#d9b65d'], ['1', '#8a6a24']], {x1: '0', y1: '0', x2: '1', y2: '1'});
    grad('rw-ball', 'radialGradient', [['0', '#ffffff'], ['0.55', '#eeeae0'], ['1', '#9b978e']], {cx: '0.35', cy: '0.3', r: '0.75'});
    grad('rw-shine', 'radialGradient', [['0', '#ffffff22'], ['1', '#ffffff00']], {cx: '0.4', cy: '0.3', r: '0.7'});

    svg('circle', {r: 198, fill: 'url(#rw-wood)'}, root);
    svg('circle', {r: 184, fill: 'none', stroke: 'url(#rw-gold)', 'stroke-width': 2}, root);
    svg('circle', {r: 182, fill: 'url(#rw-track)'}, root);
    for (let i = 0; i < 8; i++) {
      const [x, y] = polar(158, i * 45 + 22.5);
      svg('path', {d: 'M0 -6 L4 0 L0 6 L-4 0 Z', fill: 'url(#rw-gold)', transform: `translate(${x} ${y}) rotate(${i * 45 + 22.5})`}, root);
    }

    const rotor = svg('g', {class: 'wheel-rotor'}, root);
    svg('circle', {r: 153, fill: '#0b0a08', stroke: 'url(#rw-gold)', 'stroke-width': 2.5}, rotor);
    WHEEL_ORDER.forEach((number, i) => {
      const a0 = i * STEP - STEP / 2, a1 = i * STEP + STEP / 2;
      const [x0, y0] = polar(151, a0), [x1, y1] = polar(151, a1);
      const [x2, y2] = polar(104, a1), [x3, y3] = polar(104, a0);
      const fill = {red: '#b3202a', black: '#141414', green: '#15804a'}[colorOf(number)];
      svg('path', {d: `M${x0} ${y0} A151 151 0 0 1 ${x1} ${y1} L${x2} ${y2} A104 104 0 0 0 ${x3} ${y3} Z`, fill, 'data-pocket': number}, rotor);
      const [tx, ty] = polar(138, i * STEP);
      const text = svg('text', {x: tx, y: ty, transform: `rotate(${i * STEP} ${tx} ${ty})`, class: 'wheel-number'}, rotor);
      text.textContent = number;
    });
    // Pocket floor shading and frets.
    svg('circle', {r: 116, fill: 'none', stroke: '#00000055', 'stroke-width': 24}, rotor);
    svg('circle', {r: 128, fill: 'none', stroke: 'url(#rw-gold)', 'stroke-width': 1}, rotor);
    WHEEL_ORDER.forEach((_, i) => {
      const [x0, y0] = polar(151, i * STEP + STEP / 2), [x1, y1] = polar(104, i * STEP + STEP / 2);
      svg('line', {x1: x0, y1: y0, x2: x1, y2: y1, stroke: 'url(#rw-gold)', 'stroke-width': 1.4}, rotor);
    });
    svg('circle', {r: 104, fill: 'url(#rw-cone)', stroke: 'url(#rw-gold)', 'stroke-width': 2}, rotor);
    svg('circle', {r: 70, fill: 'none', stroke: '#d9b65d55', 'stroke-width': 1}, rotor);
    for (let i = 0; i < 4; i++) {
      const g = svg('g', {transform: `rotate(${i * 90})`}, rotor);
      svg('rect', {x: -3, y: -58, width: 6, height: 50, rx: 3, fill: 'url(#rw-gold)'}, g);
      svg('circle', {cx: 0, cy: -60, r: 6, fill: 'url(#rw-gold)'}, g);
    }
    svg('circle', {r: 16, fill: 'url(#rw-gold)', stroke: '#6b4f18', 'stroke-width': 1}, rotor);
    svg('circle', {r: 6, fill: '#fff1b8'}, rotor);

    const ball = svg('circle', {r: 6.5, fill: 'url(#rw-ball)', class: 'wheel-ball', visibility: 'hidden'}, root);
    svg('circle', {r: 198, fill: 'url(#rw-shine)', 'pointer-events': 'none'}, root);

    let wheelAngle = Math.random() * 360;
    let ballRel = 0;
    let ballRadius = R_REST;
    let ballVisible = false;
    let frame = null;
    let animatingId = null;
    let restingNumber = null;

    function draw() {
      rotor.setAttribute('transform', `rotate(${wheelAngle})`);
      if (ballVisible) {
        const [x, y] = polar(ballRadius, wheelAngle + ballRel);
        ball.setAttribute('cx', x.toFixed(2)); ball.setAttribute('cy', y.toFixed(2));
        ball.setAttribute('visibility', 'visible');
      } else ball.setAttribute('visibility', 'hidden');
    }

    function rest(number) {
      if (frame || number == null || number === restingNumber) return;
      restingNumber = number;
      ballVisible = true; ballRadius = R_REST;
      ballRel = WHEEL_ORDER.indexOf(number) * STEP;
      draw();
    }

    const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
    const easeOutQuart = (t) => 1 - Math.pow(1 - t, 4);
    const easeOutBounce = (t) => {
      const n = 7.5625, d = 2.75;
      if (t < 1 / d) return n * t * t;
      if (t < 2 / d) return n * (t -= 1.5 / d) * t + 0.75;
      if (t < 2.5 / d) return n * (t -= 2.25 / d) * t + 0.9375;
      return n * (t -= 2.625 / d) * t + 0.984375;
    };

    function spin(id, number, durationMs, onDone) {
      if (animatingId === id) return;
      animatingId = id;
      if (frame) cancelAnimationFrame(frame);
      const index = WHEEL_ORDER.indexOf(number);
      if (durationMs < 500) {
        frame = null; restingNumber = null; rest(number); onDone(); return;
      }
      const w0 = wheelAngle;
      const wTotal = 540 + Math.random() * 360;
      const rel0 = ballVisible ? ballRel : Math.random() * 360;
      const target = index * STEP;
      const turns = Math.max(2, Math.round(durationMs / 1100));
      const relDelta = ((((target - rel0) % 360) + 360) % 360) - 360 * turns;
      const start = performance.now();
      ballVisible = true;
      restingNumber = null;

      const tick = (now) => {
        const t = Math.min(1, (now - start) / durationMs);
        wheelAngle = w0 + wTotal * easeOutCubic(t);
        const u = Math.min(1, t / 0.86);
        ballRel = rel0 + relDelta * easeOutQuart(u);
        if (u < 0.5) ballRadius = R_TRACK;
        else {
          const drop = (u - 0.5) / 0.5;
          ballRadius = R_TRACK - (R_TRACK - R_REST) * easeOutBounce(drop);
        }
        draw();
        if (t < 1) frame = requestAnimationFrame(tick);
        else finish();
      };
      // Hidden tabs pause animation frames; this timer still lands the ball on time.
      const fallback = setTimeout(() => finish(), durationMs + 120);
      const finish = () => {
        if (frame === null && restingNumber === number) return;
        clearTimeout(fallback);
        if (frame) cancelAnimationFrame(frame);
        frame = null;
        wheelAngle = (w0 + wTotal) % 360;
        ballRel = target; ballRadius = R_REST; restingNumber = number;
        draw();
        onDone();
      };
      frame = requestAnimationFrame(tick);
    }

    draw();
    return {spin, rest, isAnimating: () => frame !== null};
  })();

  /* ------------------------------------------------------------------ */
  /* Betting board                                                       */
  /* ------------------------------------------------------------------ */
  const board = (() => {
    const numberCells = new Map();
    // Hover: show which numbers a spot covers.
    const shared = createBoard($('roulette-board'), {
      onHover: (spot) => {
        for (const cell of numberCells.values()) cell.classList.remove('covered');
        for (const n of spot?.numbers || []) numberCells.get(n)?.classList.add('covered');
      },
    });
    const { cellLayer, markerLayer, spots } = shared;
    const X0 = 70, Y0 = 10, W = 60, H = 70, D = 50, ZX = 10;
    const NUM_BOTTOM = Y0 + 3 * H;
    const cellOf = (n) => {
      const value = Number(n), c = Math.floor((value - 1) / 3), r = 2 - ((value - 1) % 3);
      return {x: X0 + c * W, y: Y0 + r * H, w: W, h: H, c, r};
    };
    const addSpot = (kind, selection, numbers, rect, label, element) => shared.addSpot(kind, selection, rect, label, element, {numbers});

    // Zero pockets (00 on top next to 3, 0 below next to 1).
    const zeroH = (3 * H) / 2;
    const zeroShapes = [
      ['00', Y0, `M${X0} ${Y0} L${ZX + 14} ${Y0} L${ZX} ${Y0 + 14} L${ZX} ${Y0 + zeroH} L${X0} ${Y0 + zeroH} Z`],
      ['0', Y0 + zeroH, `M${X0} ${Y0 + zeroH} L${ZX} ${Y0 + zeroH} L${ZX} ${NUM_BOTTOM - 14} L${ZX + 14} ${NUM_BOTTOM} L${X0} ${NUM_BOTTOM} Z`],
    ];
    for (const [n, y, d] of zeroShapes) {
      const g = svg('g', {class: 'board-cell number-cell green'}, cellLayer);
      svg('path', {d, class: 'cell-bg'}, g);
      const text = svg('text', {x: (ZX + X0) / 2, y: y + zeroH / 2, class: 'cell-number', transform: `rotate(-90 ${(ZX + X0) / 2} ${y + zeroH / 2})`}, g);
      text.textContent = n;
      numberCells.set(n, g);
      addSpot('straight', n, [n], {x: ZX, y, w: X0 - ZX, h: zeroH}, `Straight up ${n} · 35 to 1`, g);
      spots.get(`straight|${n}`).center = [(ZX + X0) / 2, y + zeroH / 2];
    }

    for (const n of ALL_NUMBERS) {
      const cell = cellOf(n);
      const g = svg('g', {class: `board-cell number-cell ${colorOf(n)}`}, cellLayer);
      svg('rect', {x: cell.x, y: cell.y, width: W, height: H, class: 'cell-bg'}, g);
      svg('ellipse', {cx: cell.x + W / 2, cy: cell.y + H / 2, rx: 17, ry: 21, class: `cell-oval ${colorOf(n)}`}, g);
      const text = svg('text', {x: cell.x + W / 2, y: cell.y + H / 2, class: 'cell-number', transform: `rotate(-90 ${cell.x + W / 2} ${cell.y + H / 2})`}, g);
      text.textContent = n;
      numberCells.set(n, g);
      addSpot('straight', n, [n], cell, `Straight up ${n} · 35 to 1`, g);
    }

    // Columns (2 to 1) on the right.
    for (let r = 0; r < 3; r++) {
      const selection = String(3 - r);
      const numbers = ALL_NUMBERS.filter((n) => (Number(n) - 1) % 3 === 3 - r - 1);
      const rect = {x: X0 + 12 * W, y: Y0 + r * H, w: W, h: H};
      const g = svg('g', {class: 'board-cell outside-cell'}, cellLayer);
      svg('rect', {x: rect.x, y: rect.y, width: rect.w, height: rect.h, class: 'cell-bg'}, g);
      const text = svg('text', {x: rect.x + W / 2, y: rect.y + H / 2, class: 'cell-label', transform: `rotate(-90 ${rect.x + W / 2} ${rect.y + H / 2})`}, g);
      text.textContent = '2 to 1';
      addSpot('column', selection, numbers, rect, `${['1st', '2nd', '3rd'][3 - r - 1]} column · 2 to 1`, g);
    }

    // Dozens.
    ['1st 12', '2nd 12', '3rd 12'].forEach((label, i) => {
      const rect = {x: X0 + i * 4 * W, y: NUM_BOTTOM, w: 4 * W, h: D};
      const g = svg('g', {class: 'board-cell outside-cell'}, cellLayer);
      svg('rect', {x: rect.x, y: rect.y, width: rect.w, height: rect.h, class: 'cell-bg'}, g);
      const text = svg('text', {x: rect.x + rect.w / 2, y: rect.y + D / 2, class: 'cell-label'}, g);
      text.textContent = label;
      const numbers = ALL_NUMBERS.filter((n) => Number(n) > i * 12 && Number(n) <= (i + 1) * 12);
      addSpot('dozen', String(i + 1), numbers, rect, `${label.replace(' 12', '')} dozen (${i * 12 + 1}–${(i + 1) * 12}) · 2 to 1`, g);
    });

    // Even-money bets.
    const evens = [
      ['low', '1 to 18', (n) => Number(n) <= 18, '1–18'],
      ['even', 'EVEN', (n) => Number(n) % 2 === 0, 'Even'],
      ['red', '', (n) => RED.has(n), 'Red'],
      ['black', '', (n) => !RED.has(n), 'Black'],
      ['odd', 'ODD', (n) => Number(n) % 2 === 1, 'Odd'],
      ['high', '19 to 36', (n) => Number(n) >= 19, '19–36'],
    ];
    evens.forEach(([kind, label, test, name], i) => {
      const rect = {x: X0 + i * 2 * W, y: NUM_BOTTOM + D, w: 2 * W, h: D};
      const g = svg('g', {class: 'board-cell outside-cell'}, cellLayer);
      svg('rect', {x: rect.x, y: rect.y, width: rect.w, height: rect.h, class: 'cell-bg'}, g);
      const cx = rect.x + rect.w / 2, cy = rect.y + D / 2;
      if (kind === 'red' || kind === 'black') {
        svg('path', {d: `M${cx - 30} ${cy} L${cx} ${cy - 15} L${cx + 30} ${cy} L${cx} ${cy + 15} Z`, class: `cell-diamond ${kind}`}, g);
      } else {
        const text = svg('text', {x: cx, y: cy, class: 'cell-label'}, g);
        text.textContent = label;
      }
      addSpot(kind, '', ALL_NUMBERS.filter(test), rect, `${name} · 1 to 1`, g);
    });

    // Inside line bets, drawn above the cells so they win the hit-test.
    const LINE = 16, CORNER = 20;
    const join = (list) => sortNumbers(list).join('-');
    for (const n of ALL_NUMBERS) {
      const value = Number(n), cell = cellOf(n);
      if (cell.c < 11) { // horizontal neighbour (n, n+3)
        const nums = [n, String(value + 3)];
        addSpot('split', join(nums), nums, {x: cell.x + W - LINE / 2, y: cell.y + 12, w: LINE, h: H - 24}, `Split ${nums.join(' / ')} · 17 to 1`);
      }
      if ((value - 1) % 3 < 2) { // vertical neighbour (n, n+1), n+1 sits above n
        const nums = [n, String(value + 1)];
        addSpot('split', join(nums), nums, {x: cell.x + 12, y: cell.y - LINE / 2, w: W - 24, h: LINE}, `Split ${nums.join(' / ')} · 17 to 1`);
        if (cell.c < 11) {
          const corner = [n, String(value + 1), String(value + 3), String(value + 4)];
          addSpot('corner', join(corner), corner, {x: cell.x + W - CORNER / 2, y: cell.y - CORNER / 2, w: CORNER, h: CORNER}, `Corner ${sortNumbers(corner).join(', ')} · 8 to 1`);
        }
      }
      if ((value - 1) % 3 === 0) { // street on the bottom edge of each column
        const street = [n, String(value + 1), String(value + 2)];
        addSpot('street', join(street), street, {x: cell.x + 12, y: NUM_BOTTOM - LINE / 2, w: W - 24, h: LINE}, `Street ${n}–${value + 2} · 11 to 1`);
        if (cell.c < 11) {
          const six = Array.from({length: 6}, (_, k) => String(value + k));
          addSpot('sixline', join(six), six, {x: cell.x + W - CORNER / 2, y: NUM_BOTTOM - LINE / 2, w: CORNER, h: LINE}, `Six line ${n}–${value + 5} · 5 to 1`);
        }
      }
    }
    // Splits against the zeros, the 0/00 split and the top line.
    const zeroSplits = [
      [['00', '3'], {x: X0 - LINE / 2, y: Y0 + 12, w: LINE, h: H - 24}],
      [['00', '2'], {x: X0 - LINE / 2, y: Y0 + H + 6, w: LINE, h: H / 2 - 12}],
      [['0', '2'], {x: X0 - LINE / 2, y: Y0 + H * 1.5 + 6, w: LINE, h: H / 2 - 12}],
      [['0', '1'], {x: X0 - LINE / 2, y: Y0 + 2 * H + 12, w: LINE, h: H - 24}],
      [['0', '00'], {x: ZX + 12, y: Y0 + zeroH - LINE / 2, w: X0 - ZX - 24, h: LINE}],
    ];
    for (const [nums, rect] of zeroSplits) addSpot('split', join(nums), nums, rect, `Split ${sortNumbers(nums).join(' / ')} · 17 to 1`);
    addSpot('topline', '', ['0', '00', '1', '2', '3'], {x: X0 - CORNER / 2, y: NUM_BOTTOM - LINE / 2, w: CORNER, h: LINE}, 'Top line 0, 00, 1, 2, 3 · 6 to 1');

    svg('rect', {x: ZX, y: Y0, width: X0 + 13 * W - ZX, height: NUM_BOTTOM - Y0, class: 'board-frame'}, cellLayer);

    function render({ownBets, otherBets, ghost, result}) {
      shared.render({ownBets, otherBets, ghost});
      markerLayer.replaceChildren();
      for (const cell of numberCells.values()) cell.classList.remove('winner');
      shared.root.querySelectorAll('.outside-cell.winner').forEach((el) => el.classList.remove('winner'));
      if (result) {
        numberCells.get(result)?.classList.add('winner');
        for (const spot of spots.values()) {
          if (spot.element.classList.contains('outside-cell') && spot.numbers.includes(result)) spot.element.classList.add('winner');
        }
        const spot = spots.get(`straight|${result}`);
        if (spot) {
          const [x, y] = spot.center;
          const dolly = svg('g', {class: 'board-dolly', transform: `translate(${x} ${y - 26})`}, markerLayer);
          svg('ellipse', {rx: 9, ry: 4, cy: 9, class: 'dolly-base'}, dolly);
          svg('rect', {x: -6, y: -6, width: 12, height: 15, rx: 3, class: 'dolly-body'}, dolly);
          svg('ellipse', {rx: 6, ry: 2.6, cy: -6, class: 'dolly-top'}, dolly);
        }
      }
    }

    return {...shared, render};
  })();

  /* ------------------------------------------------------------------ */
  /* Controls                                                            */
  /* ------------------------------------------------------------------ */
  const ownPlayer = () => state?.players.find((player) => player.sid === state.own_sid);
  const isSpinning = () => state?.phase === 'spinning' || wheel.isAnimating();
  const controls = setupControls({
    prefix: 'roulette', board, getState: () => state, isBusy: isSpinning,
    busyNotice: 'No more bets — the wheel is spinning.',
  });

  function showCallout(result) {
    const callout = $('wheel-callout');
    $('wheel-callout-number').textContent = result.number;
    $('wheel-callout-label').textContent = result.color;
    callout.className = `roulette-wheel-callout ${result.color}`;
    callout.hidden = false;
    clearTimeout(calloutTimer);
    calloutTimer = setTimeout(() => { callout.hidden = true; }, 3800);
  }

  // Results are only shown once the local wheel animation has stopped.
  function render() {
    if (!state) return;
    const next = state;
    const own = ownPlayer();
    const spinning = isSpinning();
    $('round-number').textContent = next.round_number || '—';
    $('player-count').textContent = next.players.length;
    $('table-message').textContent = next.message;
    $('join-card').hidden = Boolean(own);
    $('admin-card').hidden = !next.own_is_admin;

    const result = !spinning && next.last_result ? next.last_result : null;
    if (result) {
      $('result-number').textContent = result.number;
      $('roulette-result').className = `roulette-result ${result.color}`;
      wheel.rest(result.number);
    }
    const history = spinning && next.phase !== 'spinning' ? next.history.slice(1) : next.history;
    $('roulette-history').replaceChildren(...history.map((entry, i) => {
      const item = document.createElement('span');
      item.className = `history-pip ${entry.color}${i === 0 && !spinning ? ' latest' : ''}`;
      item.textContent = entry.number;
      return item;
    }));

    controls.render({busyLabel: 'No more bets…'});
    $('admin-forced-result').textContent = next.forced_next_result
      ? `Next result forced: ${next.forced_next_result} ${colorOf(next.forced_next_result)}`
      : 'Next result: random';
    $('admin-forced-result').classList.toggle('active', Boolean(next.forced_next_result));

    // Board chips: live bets, or the last spin's bets shown faded with the winning number.
    const live = next.players.some((p) => p.bets.length);
    const source = (p) => (live || spinning ? p.bets : p.last_bets);
    board.render({
      ownBets: own ? source(own) : [],
      otherBets: next.players.filter((p) => p.sid !== next.own_sid).flatMap(source),
      ghost: !live && !spinning,
      result: result && !live ? result.number : null,
    });
    board.setEnabled(Boolean(own) && !spinning);

    $('players').replaceChildren(...next.players.map((player) => playerCard(player, {
      ownSid: next.own_sid, busy: spinning, onRemove: controls.removeBet,
      readyBadge: (p) => (p.bets.length ? 'READY' : 'SITTING OUT'),
    })));
  }

  socket.on('connect', () => socket.emit('roulette_refresh_state'));
  socket.on('roulette_state', (next) => {
    state = next;
    if (next.phase === 'spinning' && next.spin) {
      wheel.spin(next.spin.id, next.spin.number, next.spin.remaining_ms, () => {
        showCallout({number: next.spin.number, color: next.spin.color});
        render();
      });
    }
    render();
  });
})();

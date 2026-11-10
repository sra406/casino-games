// Shared page chrome for every Royal Table page: the socket, connection badge,
// notices, refresh button and admin command box. Pages without one of these
// elements simply skip that part.
(() => {
  const $ = (id) => document.getElementById(id);
  const socket = io();
  let noticeTimer;

  function showNotice(text) {
    const notice = $('notice');
    if (!notice || !text) return;
    notice.textContent = text;
    notice.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { notice.hidden = true; }, 4200);
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  }

  // Playing cards ("10♥", or "🂠" for a face-down card) as large table cards or mini cards.
  function cardMarkup(card, large = false, extraClass = '') {
    const size = large ? 'card' : 'mini-card';
    if (card === '🂠') return `<div class="${size} hidden-card ${extraClass}">✦</div>`;
    const suit = card.slice(-1);
    const rank = card.slice(0, -1);
    const red = suit === '♥' || suit === '♦' ? 'red' : '';
    if (large) return `<div class="card ${red} ${extraClass}"><span>${rank}</span><span class="suit">${suit}</span></div>`;
    return `<div class="mini-card ${red} ${extraClass}">${rank}${suit}</div>`;
  }

  function formatCredits(value) {
    return new Intl.NumberFormat().format(Math.abs(value));
  }

  function formatCreditChange(value) {
    if (value > 0) return `+${formatCredits(value)}`;
    if (value < 0) return `−${formatCredits(value)}`;
    return '0';
  }

  // Profile photo pieces of a seat card; accounts.js handles the upload itself.
  function profileUploadMarkup(player) {
    return `<label class="profile-upload" title="JPEG, PNG, or WebP up to ${window.RoyalTable.maxProfileImageMb} MB"><input id="profile-picture-input" type="file" accept="image/jpeg,image/png,image/webp" aria-label="Upload profile picture"><span>${player.profile_image_url ? 'Change photo' : 'Add photo'}</span></label>`;
  }

  function profileStyle(player) {
    return player.profile_image_url ? `style="--profile-image: url('${escapeHtml(player.profile_image_url)}')"` : '';
  }

  function setConnection(status, label) {
    const badge = $('connection');
    if (badge) badge.className = `connection ${status}`;
    if ($('connection-label')) $('connection-label').textContent = label;
    if ($('refresh-button')) $('refresh-button').disabled = status !== 'online';
  }

  // An admin disconnect is final for this page: Socket.IO won't reconnect on its own,
  // so say why instead of leaving the header on "Reconnecting…".
  let disconnectedByAdmin = null;
  socket.on('connect', () => setConnection('online', 'Connected'));
  socket.on('connect_error', () => setConnection('offline', 'Connection trouble'));
  socket.on('notice', (data) => showNotice(data?.text));
  socket.on('admin_disconnected', (data) => {
    disconnectedByAdmin = data?.text || 'An admin disconnected you from the table.';
    showNotice(disconnectedByAdmin);
  });
  socket.on('disconnect', (reason) => {
    if (reason === 'io server disconnect') {
      setConnection('offline', 'Disconnected · reload to rejoin');
      showNotice(disconnectedByAdmin || 'The server closed your connection. Reload the page to rejoin.');
    } else {
      setConnection('offline', 'Reconnecting…');
    }
  });

  // Refresh: re-request the live table (event named by the page) and reload the
  // locally hosted background and button images without leaving the seat.
  const refreshButton = $('refresh-button');
  if (refreshButton) {
    const icon = $('refresh-icon');
    const showIcon = (loaded) => {
      icon.hidden = !loaded;
      $('refresh-fallback').hidden = loaded;
      refreshButton.classList.toggle('has-image', loaded);
    };
    icon.addEventListener('load', () => showIcon(true));
    icon.addEventListener('error', () => showIcon(false));
    refreshButton.addEventListener('click', () => {
      const key = Date.now();
      document.querySelector('.table-panel')?.style.setProperty('--table-background-image', `url("/table-background?refresh=${key}")`);
      icon.src = `/refresh-button-image?refresh=${key}`;
      const event = document.body.dataset.refreshEvent;
      if (event) socket.emit(event);
    });
  }

  // Admin timer and manual-draw panel (templates/partials/admin-table-controls.html).
  let adminState = null;
  const runAdmin = (command) => socket.emit('admin_command', { command });
  function renderAdminControls(state) {
    adminState = state;
    if (!$('manual-draw-toggle') || !state) return;
    $('admin-timer-status').textContent = state.timer_remaining == null
      ? (state.timer_paused ? 'Paused' : 'Waiting for a decision')
      : `${state.timer_remaining}s${state.timer_paused ? ' · paused' : ''}`;
    $('timer-toggle').textContent = state.timer_paused ? 'Resume timer' : 'Pause timer';
    $('manual-draw-status').textContent = state.manual_draw_enabled ? 'On' : 'Off';
    $('manual-draw-toggle').textContent = state.manual_draw_enabled ? 'Disable manual draws' : 'Enable manual draws';
    $('admin-deck-top-wrap').hidden = !state.manual_draw_enabled;
    $('admin-deck-top').innerHTML = state.admin_deck_top ? cardMarkup(state.admin_deck_top, true) : '—';
    $('confirm-draw-button').hidden = !state.admin_draw_pending;
    $('admin-draw-target').hidden = !state.admin_draw_pending;
    $('admin-draw-target').textContent = state.admin_draw_target ? `Pending: ${state.admin_draw_target}` : '';
  }
  if ($('manual-draw-toggle')) {
    $('timer-toggle').addEventListener('click', () => runAdmin(adminState?.timer_paused ? '/timer resume' : '/timer pause'));
    $('manual-draw-toggle').addEventListener('click', () => runAdmin(adminState?.manual_draw_enabled ? '/manualdraw off' : '/manualdraw on'));
    $('confirm-draw-button').addEventListener('click', () => runAdmin('/draw'));
  }

  // The admin terminal opens over the game in a frame, so this page's connection
  // (and the admin's seat) stays put. The frame shares this tab's sign-in.
  const terminalDialog = $('admin-terminal-dialog');
  if (terminalDialog) {
    $('admin-terminal-open').addEventListener('click', () => {
      const frame = $('admin-terminal-frame');
      if (!frame.getAttribute('src')) frame.src = '/admin?embedded=1';
      terminalDialog.showModal();
    });
    $('admin-terminal-close').addEventListener('click', () => terminalDialog.close());
  }

  const adminForm = $('admin-command-form');
  if (adminForm) {
    adminForm.addEventListener('submit', (event) => {
      event.preventDefault();
      const input = $('admin-command-input');
      socket.emit('admin_command', { command: input.value.trim() });
      input.value = '';
    });
  }

  window.RoyalTable = {
    socket, $, showNotice, escapeHtml, cardMarkup, formatCredits, formatCreditChange, profileUploadMarkup, profileStyle,
    renderAdminControls,
  };
})();

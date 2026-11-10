// Sign-in forms, session resume and profile pictures for the game pages.
(() => {
  const { socket, $, showNotice: notify } = window.RoyalTable;
  const sessionStorageKey = 'royal-table-account-session';
  let authMode = 'login';
  const maxProfileImageMb = Number($('join-card')?.dataset.maxProfileImageMb) || 5;
  window.RoyalTable.maxProfileImageMb = maxProfileImageMb;

  socket.on('connect', () => {
    const token = sessionStorage.getItem(sessionStorageKey);
    if (token) socket.emit('resume_account', { token, game: document.body.dataset.game || 'blackjack' });
  });
  socket.on('account_session', (data) => {
    if (data?.token) sessionStorage.setItem(sessionStorageKey, data.token);
  });
  socket.on('account_session_invalid', (data) => {
    if (sessionStorage.getItem(sessionStorageKey) === data?.token) sessionStorage.removeItem(sessionStorageKey);
  });

  function setAuthMode(mode) {
    authMode = mode;
    const registering = mode === 'register';
    const temporary = mode === 'temporary';
    $('auth-title').textContent = temporary ? 'Play temporarily' : registering ? 'Create account' : 'Sign in';
    $('auth-submit').textContent = temporary ? 'Play temporarily' : registering ? 'Create account' : 'Sign in';
    $('login-tab').classList.toggle('selected', mode === 'login');
    $('register-tab').classList.toggle('selected', registering);
    $('temporary-tab').classList.toggle('selected', temporary);
    $('password-fields').hidden = temporary;
    $('password').required = !temporary;
    $('password').autocomplete = registering ? 'new-password' : 'current-password';
    $('auth-note').textContent = temporary
      ? 'Temporary account data is deleted after you leave; switching games keeps your seat briefly so your session can follow.'
      : 'Permanent account data is saved in MySQL. Your sign-in follows you between games in this tab.';
  }

  if ($('auth-form')) {
    $('login-tab').addEventListener('click', () => setAuthMode('login'));
    $('register-tab').addEventListener('click', () => setAuthMode('register'));
    $('temporary-tab').addEventListener('click', () => setAuthMode('temporary'));
    $('auth-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const game = document.body.dataset.game || 'blackjack';
      if (authMode === 'temporary') {
        socket.emit('temporary', { username: $('username').value.trim(), game });
      } else {
        socket.emit(authMode === 'register' ? 'register' : 'login', {
          username: $('username').value.trim(),
          password: $('password').value,
          game,
        });
      }
    });
  }

  const players = $('players');
  if (players) {
    players.addEventListener('change', async (event) => {
      const input = event.target;
      if (input.id !== 'profile-picture-input' || !input.files || !input.files[0]) return;
      const file = input.files[0];
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
        notify('Choose a JPEG, PNG, or WebP image.');
        input.value = '';
        return;
      }
      if (file.size > maxProfileImageMb * 1024 * 1024) {
        notify(`Profile pictures must be ${maxProfileImageMb} MB or smaller.`);
        input.value = '';
        return;
      }
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error('Could not read the selected image.'));
          reader.readAsDataURL(file);
        });
        socket.emit('profile_picture', { data_url: dataUrl });
      } catch (error) {
        notify(error.message);
      }
      input.value = '';
    });
  }
})();

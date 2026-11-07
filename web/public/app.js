'use strict';
const $ = (selector) => document.querySelector(selector);
const state = {user: null, conversations: [], current: null, busy: false, signup: false, view: 0, deleteId: null, deleting: false};

function showError(target, message = '') { target.textContent = message; target.hidden = !message; }

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin', ...options,
    headers: {'Content-Type': 'application/json', ...options.headers},
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/auth/')) showAuth();
    let detail = data.detail;
    if (Array.isArray(detail)) detail = detail.map(item => item.msg.replace(/^Value error, /, '')).join('. ');
    throw new Error(typeof detail === 'string' ? detail : 'Something went wrong. Please try again.');
  }
  return data;
}

function toggleMode(signup) {
  state.signup = signup;
  $('#login-tab').classList.toggle('selected', !signup);
  $('#signup-tab').classList.toggle('selected', signup);
  $('#name-field').hidden = !signup;
  $('#name').required = signup;
  $('#password').minLength = signup ? 12 : 1;
  $('#password').autocomplete = signup ? 'new-password' : 'current-password';
  $('#password-help').hidden = !signup;
  $('#auth-title').textContent = signup ? 'Make yourself at home.' : 'Good to see you.';
  $('#auth-subtitle').textContent = signup ? 'A thinking space of your own, in a few seconds.' : 'Sign in and pick up where you left off.';
  $('#auth-submit').textContent = signup ? 'Create your account ↗' : 'Sign in ↗';
  showError($('#auth-error'));
}

function showAuth() {
  state.user = null; state.current = null; state.conversations = []; state.view++;
  $('#messages').replaceChildren(); $('#conversation-list').replaceChildren();
  $('#message').value = ''; $('#search').value = ''; $('#account-name').textContent = ''; $('#account-email').textContent = '';
  $('#workspace').hidden = true; $('#auth').hidden = false; $('#boot').hidden = true;
  setSidebar(false);
}

function updateUsage(data) {
  $('#usage-text').textContent = `${data.usage} / ${data.limit}`;
  $('#usage-bar').max = data.limit; $('#usage-bar').value = data.usage;
}

async function enterWorkspace() {
  const me = await api('/api/auth/me');
  state.user = me.user;
  $('#account-name').textContent = me.user.name;
  $('#account-email').textContent = me.user.email;
  $('#avatar').textContent = me.user.name.slice(0, 1).toUpperCase();
  updateUsage(me);
  $('#auth').hidden = true; $('#workspace').hidden = false; $('#boot').hidden = true;
  $('#password').value = '';
  newChat();
  await refreshConversations();
}

async function refreshConversations() {
  const accountId = state.user?.id;
  const rows = await api('/api/conversations');
  if (state.user?.id !== accountId) return;
  state.conversations = rows;
  renderConversations();
}

function renderConversations() {
  const list = $('#conversation-list'); list.replaceChildren();
  $('#chat-count').textContent = String(state.conversations.length);
  const query = $('#search').value.toLowerCase();
  const rows = state.conversations.filter(item => item.title.toLowerCase().includes(query));
  if (!rows.length) {
    const empty = document.createElement('p'); empty.className = 'empty-list';
    empty.textContent = query ? 'No matching conversations.' : 'Your next good idea starts here. Send a message to begin.';
    list.append(empty);
  }
  for (const item of rows) {
    const row = document.createElement('div'); row.className = 'conversation-row';
    row.classList.toggle('active', state.current === item.id);
    const open = document.createElement('button'); open.className = 'conversation-open';
    open.textContent = item.title; open.title = item.title;
    open.setAttribute('aria-current', state.current === item.id ? 'page' : 'false');
    open.disabled = state.busy; open.addEventListener('click', () => openConversation(item));
    const remove = document.createElement('button'); remove.className = 'conversation-delete';
    remove.textContent = '×'; remove.setAttribute('aria-label', `Delete ${item.title}`);
    remove.disabled = state.busy;
    remove.addEventListener('click', () => {
      if (state.deleting) return;
      state.deleteId = item.id;
      state.view++; // Invalidate a history load started before the dialog opened.
      showError($('#delete-conversation-error'));
      $('#delete-dialog').showModal();
    });
    row.append(open, remove); list.append(row);
  }
}

function setSidebar(open) {
  $('#sidebar').classList.toggle('open', open);
  $('#sidebar-shade').hidden = !open;
  $('#menu').setAttribute('aria-expanded', String(open));
  $('#sidebar').inert = window.matchMedia('(max-width: 700px)').matches && !open;
}
window.matchMedia('(max-width: 700px)').addEventListener('change', () => setSidebar(false));

function newChat() {
  if (state.busy) return;
  state.current = null; state.view++;
  $('#messages').replaceChildren(); $('#welcome').hidden = false;
  $('#chat-title').textContent = 'New conversation'; $('#message').value = '';
  showError($('#chat-error')); resizeComposer(); renderConversations(); setSidebar(false);
}

function addMessage(role, content, pending = false) {
  const node = document.createElement('article'); node.className = `message ${role}${pending ? ' pending' : ''}`;
  const meta = document.createElement('div'); meta.className = 'message-meta';
  if (role === 'assistant') {
    const mark = document.createElement('span'); mark.className = 'brand-mark'; mark.textContent = 'j.';
    meta.append(mark, document.createTextNode('JARVIS'));
  } else meta.textContent = 'YOU';
  const body = document.createElement('div'); body.className = `message-body${pending ? ' pending-dot' : ''}`;
  // Deliberately render model/user content as text, never executable HTML.
  body.textContent = content;
  node.append(meta, body); $('#messages').append(node);
  return node;
}

function scrollBottom() { $('#chat-scroll').scrollTop = $('#chat-scroll').scrollHeight; }

async function openConversation(item) {
  if (state.busy) return;
  const view = ++state.view;
  setSidebar(false); showError($('#chat-error'));
  try {
    const messages = await api(`/api/conversations/${item.id}/messages`);
    if (view !== state.view || !state.user) return;
    state.current = item.id; $('#chat-title').textContent = item.title;
    $('#messages').replaceChildren(); $('#welcome').hidden = messages.length > 0;
    for (const message of messages) addMessage(message.role, message.content);
    renderConversations(); scrollBottom();
  } catch (error) { if (view === state.view) showError($('#chat-error'), error.message); }
}

function setBusy(busy) {
  state.busy = busy;
  for (const selector of ['#send', '#new-chat', '#logout', '#message']) $(selector).disabled = busy;
  document.querySelectorAll('[data-prompt]').forEach(button => { button.disabled = busy; });
  $('#send').textContent = busy ? '…' : '↑';
  renderConversations();
}

function resizeComposer() { $('#message').style.height = 'auto'; $('#message').style.height = `${Math.min($('#message').scrollHeight, 180)}px`; }

$('#login-tab').addEventListener('click', () => toggleMode(false));
$('#signup-tab').addEventListener('click', () => toggleMode(true));
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); showError($('#auth-error'));
  const button = $('#auth-submit'); button.disabled = true;
  const payload = {email: $('#email').value, password: $('#password').value};
  if (state.signup) payload.name = $('#name').value;
  try {
    await api(`/api/auth/${state.signup ? 'signup' : 'login'}`, {method: 'POST', body: JSON.stringify(payload)});
    await enterWorkspace();
  } catch (error) { showError($('#auth-error'), error.message); }
  finally { button.disabled = false; }
});
$('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', {method: 'POST', body: '{}'}); showAuth(); }
  catch (error) { showError($('#chat-error'), error.message); }
});
$('#new-chat').addEventListener('click', newChat);
$('#search').addEventListener('input', renderConversations);
$('#menu').addEventListener('click', () => setSidebar(!$('#sidebar').classList.contains('open')));
$('#sidebar-shade').addEventListener('click', () => setSidebar(false));
document.addEventListener('keydown', event => { if (event.key === 'Escape') setSidebar(false); });
$('#delete-conversation-cancel').addEventListener('click', () => {
  if (!state.deleting) $('#delete-dialog').close();
});
$('#delete-dialog').addEventListener('cancel', event => {
  if (state.deleting) event.preventDefault();
});
$('#delete-dialog').addEventListener('close', () => { state.deleteId = null; });
$('#delete-conversation-submit').addEventListener('click', async () => {
  if (state.deleting || !state.deleteId) return;
  const id = state.deleteId;
  state.deleting = true;
  const button = $('#delete-conversation-submit');
  button.disabled = true;
  button.textContent = 'Deleting…';
  $('#delete-conversation-cancel').disabled = true;
  $('#delete-dialog').setAttribute('aria-busy', 'true');
  showError($('#delete-conversation-error'));
  try {
    await api(`/api/conversations/${id}`, {method: 'DELETE', body: '{}'});
    state.conversations = state.conversations.filter(item => item.id !== id);
    if (state.current === id) newChat();
    else renderConversations();
    $('#delete-dialog').close();
  } catch (error) {
    if (state.user) showError($('#delete-conversation-error'), error.message);
    else $('#delete-dialog').close();
  } finally {
    state.deleting = false;
    button.disabled = false;
    button.textContent = 'Delete conversation';
    $('#delete-conversation-cancel').disabled = false;
    $('#delete-dialog').removeAttribute('aria-busy');
  }
});
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
  $('#message').value = button.dataset.prompt; resizeComposer(); $('#message').focus();
}));
$('#message').addEventListener('input', resizeComposer);
$('#message').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('#composer').requestSubmit(); }
});
$('#composer').addEventListener('submit', async event => {
  event.preventDefault();
  const message = $('#message').value.trim();
  if (!message || state.busy) return;
  state.view++; setBusy(true); showError($('#chat-error'));
  let pending, optimistic;
  let completed = false;
  try {
    if (!state.current) {
      const conversation = await api('/api/conversations', {method: 'POST', body: '{}'});
      state.current = conversation.id; state.conversations.unshift(conversation); renderConversations();
    }
    $('#welcome').hidden = true; $('#message').value = ''; resizeComposer();
    optimistic = addMessage('user', message);
    pending = addMessage('assistant', 'Thinking it through…', true); scrollBottom();
    const data = await api(`/api/conversations/${state.current}/messages`, {method: 'POST', body: JSON.stringify({message})});
    pending.remove(); pending = null;
    addMessage('assistant', data.response); completed = true; updateUsage(data); scrollBottom();
    await refreshConversations();
    $('#chat-title').textContent = state.conversations.find(item => item.id === state.current)?.title || 'Conversation';
  } catch (error) {
    pending?.remove();
    if (!completed) {
      optimistic?.remove();
      if (state.user) $('#message').value = message;
      $('#welcome').hidden = $('#messages').children.length > 0;
    }
    showError($('#chat-error'), error.message); resizeComposer();
    if (state.user) api('/api/auth/me').then(updateUsage).catch(() => {});
  } finally { setBusy(false); if (state.user) $('#message').focus(); }
});

async function boot() {
  // The desktop app's older service worker must not cache account pages.
  if ('serviceWorker' in navigator) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map(registration => registration.unregister()));
    } catch { /* Private browsing may disable this API; account access still works. */ }
  }
  try {
    const health = await api('/api/health');
    $('#signup-tab').hidden = !health.signup_enabled;
    $('#configuration-notice').hidden = health.ai_configured;
    $('#ai-status').classList.toggle('unavailable', !health.ai_configured);
    if (!health.ai_configured) $('#ai-status').lastChild.textContent = ' Setup pending';
    try { await enterWorkspace(); }
    catch { showAuth(); }
  } catch {
    showAuth(); showError($('#auth-error'), 'Cannot connect to the server. Refresh the page to try again.');
  }
}
boot().catch(() => { showAuth(); showError($('#auth-error'), 'Could not open your workspace. Please refresh.'); });

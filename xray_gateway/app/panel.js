
'use strict';

const $ = selector => document.querySelector(selector);
const network = $('#network-form');
const networkDialog = $('#network-dialog');
const importDialog = $('#import-dialog');
const subscription = $('#subscription-form');
const profileForm = $('#profile-form');
const radios = { '2.4': [1,2,3,4,5,6,7,8,9,10,11,12,13],
  '5': [36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144,149,153,157,161,165] };
const dfsChannels = new Set([52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144]);

let state;
let catalog;
let busy = false;
let pinging = false;
let refreshing = false;
let results = new Map();
let radioRequest = 0;
let capabilities;
let noticeTimer;
let statusRequest = 0;
const field = name => network.elements.namedItem(name);

async function api(path, data) {
  const init = data === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
  };
  const response = await fetch('api/' + path, { ...init, cache: 'no-store' });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body) {
    throw new Error(body?.error || 'Откройте панель из Home Assistant и обновите страницу');
  }
  return body;
}

function showNotice(message, error = false) {
  const notice = $('#notice');
  clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.classList.toggle('error', error);
  notice.hidden = !message;
  if (message) noticeTimer = setTimeout(() => { notice.hidden = true }, error ? 11000 : 4400);
}

function formError(form, message = '') {
  const error = form.querySelector('[data-error]');
  error.hidden = !message;
  error.textContent = message;
}

function currentProfile() {
  return catalog?.profiles.find(profile => profile.id === catalog.selected);
}

function renderGateway() {
  const status = $('#status');
  const power = $('#power');
  const unavailable = !state || busy;
  const profile = currentProfile();
  const phase = state?.running ? 'running' : state?.starting ? 'starting' : state?.needs_stop ? 'failed' : 'off';
  status.dataset.state = phase;
  status.textContent = {
    running: 'Шлюз запущен',
    starting: state?.wifi_status || 'Точка доступа запускается…',
    failed: 'Шлюз требует остановки',
    off: state ? 'Шлюз выключен' : 'Подключение к панели…'
  }[phase];
  $('#connection-info').textContent = [
    profile?.name || (state?.profile_present ? 'Сохранённый профиль' : ''),
    state?.settings.downstream && (state.settings.client_service === 'wifi_ap'
      ? state.settings.wifi_ssid : state.settings.downstream)
  ].filter(Boolean).join(' · ');
  const error = $('#gateway-error');
  error.textContent = state?.error || '';
  error.hidden = !error.textContent;
  power.dataset.stop = String(Boolean(state?.needs_stop));
  power.disabled = unavailable || (!state.needs_stop && (!state.profile_present || !state.settings.downstream));
  $('#power-label').textContent = state?.needs_stop ? 'Остановить' : 'Включить шлюз';
  $('#network-open').disabled = unavailable || !!state.needs_stop;
  $('#import-open').disabled = unavailable || !catalog || !!state.needs_stop;
  $('#catalog-refresh').hidden = !catalog?.configured;
  $('#catalog-refresh').disabled = unavailable || refreshing || !!state.needs_stop;
  $('#catalog-refresh').textContent = refreshing ? 'Обновляется…' : 'Обновить';
}

function actionButton(label, onClick, disabled = false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener('click', onClick);
  return button;
}

function renderProfiles() {
  const list = $('#profiles');
  list.replaceChildren();
  if (!state || !catalog) return;
  const rows = [...catalog.profiles];
  if (state.profile_present && catalog.selected === null) {
    rows.unshift({ id: null, name: 'Сохранённый профиль' });
  }
  for (const row of rows) {
    const selected = row.id === catalog.selected;
    const item = document.createElement('div');
    item.className = selected ? 'profile selected' : 'profile';
    const title = document.createElement('div');
    title.className = 'profile-name';
    const name = document.createElement('strong');
    name.textContent = row.name;
    title.append(name);
    if (selected) {
      const label = document.createElement('small');
      label.textContent = 'Выбран';
      title.append(label);
    }
    const actions = document.createElement('div');
    actions.className = 'buttons';
    const result = results.get(row.id);
    const ping = actionButton(result?.label ?? 'Пинг', () => pingProfile(row), busy || pinging);
    ping.setAttribute('aria-label', 'Проверить задержку · ' + row.name);
    if (result) {
      ping.classList.add(result.type);
      ping.title = result.detail;
    }
    actions.append(ping);
    if (!selected) actions.append(actionButton('Выбрать', () => selectProfile(row), busy || state.needs_stop));
    item.append(title, actions);
    list.append(item);
  }
  $('#profile-hint').textContent = !rows.length
    ? 'Добавьте ссылку на подписку или импортируйте Xray JSON.'
    : !state.settings.downstream && !state.needs_stop
      ? 'Выберите клиентский интерфейс в настройках сети.'
      : state.needs_stop && rows.length > 1
        ? 'Чтобы сменить профиль, остановите шлюз.'
        : '';
}

function render() {
  renderGateway();
  renderProfiles();
}

async function refreshStatus() {
  const request = ++statusRequest;
  const next = await api('status');
  if (request !== statusRequest) return;
  const changed = state?.needs_stop !== next.needs_stop || state?.profile_present !== next.profile_present;
  state = next;
  renderGateway();
  if (changed) renderProfiles();
}

async function refreshCatalog() {
  catalog = await api('subscription');
  render();
}

async function perform(task, form) {
  if (busy) return;
  busy = true;
  statusRequest++;
  if (form) {
    formError(form);
    form.querySelector('[type="submit"]').disabled = true;
  }
  render();
  try {
    await task();
  } catch (error) {
    if (form) formError(form, error.message);
    else showNotice(error.message, true);
  } finally {
    busy = false;
    if (form) form.querySelector('[type="submit"]').disabled = false;
    render();
  }
}

async function pingProfile(row) {
  if (pinging || busy) return;
  pinging = true;
  const measurements = results;
  measurements.set(row.id, { label: 'Проверка…', type: 'pending', detail: 'Запрос HTTPS через профиль' });
  renderProfiles();
  try {
    const result = await api('ping', row.id === null ? {} : { id: row.id, revision: row.revision });
    measurements.set(row.id, { label: result.milliseconds + ' мс', type: 'ping-ok', detail: 'До ответа HTTPS через профиль' });
    showNotice(row.name + ' · ' + result.milliseconds + ' мс');
  } catch (error) {
    measurements.set(row.id, { label: 'Повторить пинг', type: 'ping-failed', detail: error.message });
    showNotice(row.name + ' · ' + error.message, true);
  } finally {
    pinging = false;
    renderProfiles();
  }
}

function selectProfile(row) {
  perform(async () => {
    await api('subscription/select', { id: row.id, revision: row.revision });
    await refreshStatus();
    await refreshCatalog();
  });
}

$('#power').onclick = () => perform(async () => {
  const stop = state.needs_stop;
  $('#power-label').textContent = stop ? 'Остановка…' : 'Запуск…';
  try {
    await api(stop ? 'stop' : 'start', {});
  } finally {
    await refreshStatus().catch(() => {});
  }
});

async function watchRefresh() {
  if (refreshing) return;
  refreshing = true;
  renderGateway();
  try {
    while (true) {
      const result = await api('subscription/refresh');
      if (result.state === 'error') throw new Error(result.error);
      if (result.state === 'done') {
        results = new Map();
        await refreshCatalog();
        await refreshStatus();
        const details = ['Загружено: ' + result.count];
        if (result.skipped) details.push('пропущено: ' + result.skipped);
        if (result.selection_lost) details.push('предыдущий профиль сохранён отдельно');
        showNotice(details.join(' · '));
        break;
      }
      if (result.state !== 'running') break;
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    refreshing = false;
    renderGateway();
  }
}

$('#catalog-refresh').onclick = () => perform(async () => {
  await api('subscription/refresh', {});
  watchRefresh();
});

function populateInterfaces(name, items, wanted) {
  const select = field(name);
  select.replaceChildren(new Option('Выберите интерфейс', ''));
  for (const item of items) {
    const label = item.name + (item.wifi ? ' · Wi-Fi' : '') + (item.is_default ? ' · WAN' : '');
    select.add(new Option(label, item.name));
  }
  select.value = items.some(item => item.name === wanted) ? wanted : '';
}

function updateNetworkMode(preferred = field('downstream').value) {
  const mode = field('client_service').value;
  const wifi = mode === 'wifi_ap';
  populateInterfaces('downstream', state.interfaces.filter(item => !item.is_default && (!wifi || item.wifi)), preferred);
  $('#dhcp-fields').hidden = mode === 'manual';
  $('#wifi-fields').hidden = !wifi;
  $('#wifi-advanced').hidden = !wifi;
  $('#mode-hint').textContent = {
    dhcp: 'Выберите выделенный порт для устройств или внешней точки доступа.',
    wifi_ap: 'Адаптер должен поддерживать режим точки доступа.',
    manual: 'Адрес шлюза должен быть назначен интерфейсу в Home Assistant.'
  }[mode];
}

function updateChannels(preferred = field('wifi_channel').value) {
  const band = field('wifi_band').value;
  const select = field('wifi_channel');
  const country = field('wifi_country').value.trim().toUpperCase();
  const changeCountry = field('wifi_apply_regdomain').checked && /^[A-Z]{2}$/.test(country);
  const dfs = changeCountry && field('wifi_allow_dfs').checked;
  const canRequestCountry = changeCountry && capabilities?.global_country
    && country !== capabilities.global_country && !capabilities.self_managed;
  select.replaceChildren();
  for (const channel of radios[band]) {
    const frequency = band === '5' ? 5000 + 5 * channel : 2407 + 5 * channel;
    const actual = capabilities?.channels.find(item => item.channel === channel && Math.abs(item.frequency_mhz - frequency) < .5);
    const radar = band === '5' && (dfsChannels.has(channel) || actual?.radar);
    const noIr = actual?.no_ir && !(actual.radar && dfs && !capabilities.self_managed);
    const reason = capabilities?.supports_ap === false ? 'Режим AP недоступен'
      : capabilities && !actual ? 'Канал отсутствует'
      : actual?.disabled ? 'Отключён драйвером'
      : noIr && !canRequestCountry ? 'no-IR'
      : radar && !dfs ? 'Требуется DFS' : '';
    const suffix = reason || (noIr ? 'Проверка после смены региона' : radar ? 'DFS' : '');
    const option = new Option(String(channel) + (suffix ? ' · ' + suffix : ''), String(channel));
    option.disabled = Boolean(reason);
    select.add(option);
  }
  const options = [...select.options];
  const allowed = options.filter(option => !option.disabled);
  select.value = options.find(option => option.value === String(preferred))?.value || allowed[0]?.value || '';
  const chosen = select.selectedOptions[0];
  const noIr = options.some(option => option.disabled && option.textContent.includes('no-IR'));
  $('#channel-hint').textContent = capabilities?.supports_ap === false
    ? 'Адаптер не поддерживает режим точки доступа.'
    : !allowed.length && band === '5'
      ? (noIr ? 'На 5 ГГц драйвер запрещает начать передачу (no-IR). Выберите 2,4 ГГц или внешнюю точку доступа.'
              : 'Нет доступных каналов 5 ГГц. Выберите 2,4 ГГц или внешнюю точку доступа.')
      : chosen?.disabled ? 'Выбранный канал недоступен. Выберите разрешённый канал.'
      : '';
}

async function loadRadio(preferred) {
  const request = ++radioRequest;
  capabilities = null;
  updateChannels(preferred);
  const device = field('downstream').value;
  if (field('client_service').value !== 'wifi_ap' || !device) return;
  try {
    const result = await api('wifi-capabilities?device=' + encodeURIComponent(device));
    if (request !== radioRequest || !networkDialog.open) return;
    capabilities = result;
    updateChannels(field('wifi_channel').value);
  } catch (error) {
    if (request === radioRequest && networkDialog.open) $('#channel-hint').textContent = error.message;
  }
}

$('#network-open').onclick = () => {
  formError(network);
  const settings = state.settings;
  for (const item of network.elements) {
    if (!(item.name in settings)) continue;
    if (item.type === 'checkbox') item.checked = Boolean(settings[item.name]);
    else item.value = settings[item.name];
  }
  field('wifi_password').value = '';
  field('wifi_password').placeholder = settings.wifi_password_saved ? 'Пароль сохранён' : '8–63 символа ASCII';
  if (!settings.downstream) field('client_service').value = 'dhcp';
  const wanItems = state.interfaces.filter(item => item.is_default);
  populateInterfaces('upstream', wanItems, settings.upstream || wanItems[0]?.name);
  updateNetworkMode(settings.downstream);
  networkDialog.showModal();
  loadRadio(settings.wifi_channel);
};
field('client_service').onchange = () => {
  updateNetworkMode();
  loadRadio();
};
field('downstream').onchange = () => loadRadio();
for (const name of ['wifi_band', 'wifi_country', 'wifi_apply_regdomain', 'wifi_allow_dfs']) {
  field(name).onchange = () => updateChannels();
}
network.onsubmit = event => {
  event.preventDefault();
  perform(async () => {
    const data = Object.fromEntries(new FormData(network));
    data.block_ipv6 = state.settings.block_ipv6;
    data.port = Number(data.port);
    if (data.client_service === 'wifi_ap' && (!data.wifi_channel || field('wifi_channel').selectedOptions[0]?.disabled)) {
      throw new Error('Выберите доступный канал Wi-Fi');
    }
    data.wifi_channel = Number(data.wifi_channel) || state.settings.wifi_channel;
    data.wifi_country = data.wifi_country.trim().toUpperCase();
    for (const name of ['wifi_11n', 'wifi_apply_regdomain', 'wifi_allow_dfs']) data[name] = field(name).checked;
    await api('settings', data);
    results = new Map();
    networkDialog.close();
    await refreshStatus();
    showNotice('Настройки сети сохранены');
  }, network);
};

$('#import-open').onclick = () => {
  for (const form of [subscription, profileForm]) {
    form.reset();
    formError(form);
  }
  subscription.elements.url.placeholder = catalog.configured ? 'Ссылка сохранена' : 'https://…';
  subscription.elements.user_agent.value = catalog.user_agent;
  subscription.elements.remnawave.checked = catalog.remnawave;
  importDialog.showModal();
};
subscription.onsubmit = event => {
  event.preventDefault();
  perform(async () => {
    const data = Object.fromEntries(new FormData(subscription));
    data.url = data.url.trim();
    if (!data.url && !catalog.configured) throw new Error('Вставьте URL подписки');
    if (data.url) {
      await api('subscription', { ...data, remnawave: subscription.elements.remnawave.checked });
      await refreshCatalog();
      subscription.elements.url.value = '';
    }
    await api('subscription/refresh', {});
    importDialog.close();
    watchRefresh();
  }, subscription);
};
$('#profile-file').onchange = async event => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    formError(profileForm);
    if (file.size >= 2 * 1024 * 1024) throw new Error('Выберите JSON-файл меньше 2 МБ');
    profileForm.elements.profile.value = (await file.text()).replace(/^\uFEFF/, '');
  } catch (error) {
    formError(profileForm, error.message);
  }
};
profileForm.onsubmit = event => {
  event.preventDefault();
  perform(async () => {
    let profile;
    try { profile = JSON.parse(profileForm.elements.profile.value) }
    catch { throw new Error('Вставьте полный Xray JSON или выберите JSON-файл') }
    await api('profile', { profile });
    results = new Map();
    profileForm.reset();
    importDialog.close();
    await refreshStatus();
    await refreshCatalog();
    showNotice('Профиль сохранён');
  }, profileForm);
};

for (const dialog of [networkDialog, importDialog]) {
  dialog.querySelector('[data-close]').onclick = () => { if (!busy) dialog.close() };
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault() });
}

Promise.all([refreshStatus(), refreshCatalog()]).then(async () => {
  if ((await api('subscription/refresh')).state === 'running') watchRefresh();
}).catch(error => showNotice(error.message, true));
setInterval(() => {
  if (busy || document.hidden) return;
  refreshStatus().then(() => {
    if (!catalog) return refreshCatalog();
  }).catch(() => {});
}, 7000);

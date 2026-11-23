(() => {
  const loginView = document.getElementById('login-view');
  const adminView = document.getElementById('admin-view');
  const loginForm = document.getElementById('login-form');
  const loginMessage = document.getElementById('login-message');
  const botsList = document.getElementById('bots-list');
  const grantViews = new Map();
  let grants = [];
  const notice = document.getElementById('notice');
  const integrationsUrl = '/api/v1/admin/notification-integrations';
  const createButton = document.getElementById('create-button');
  const helpDialog = document.getElementById('help-dialog');
  const helpContent = document.getElementById('help-content');
  const dialogs = {
    botx: document.getElementById('create-botx'),
    bots: document.getElementById('create-internal-bot'),
    integrations: document.getElementById('create-integration'),
  };
  const integrationsList = document.getElementById('integrations-list');
  const issuedToken = document.getElementById('issued-token');
  const sections = {
    botx: {
      element: document.getElementById('botx-section'),
      title: 'BotX',
      description: 'Подключения к eXpress и обработчики входящих сообщений.',
    },
    bots: {
      element: document.getElementById('bots-section'),
      title: 'Внутренние боты',
      description: 'Типы, обработка чатов и доступ пользователей.',
    },
    integrations: {
      element: document.getElementById('integrations-section'),
      title: 'Внешние системы',
      description: 'Маршруты отправки сообщений, токены и журнал доставки.',
    },
  };
  let bots = [];
  let internalBots = [];
  let templates = [];
  let integrations = [];
  let activeSection = 'botx';

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'include',
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
    });
    if (response.status === 401 && path !== '/api/v1/admin/login') {
      showLogin();
      throw new Error('Сессия завершена. Войдите снова.');
    }
    if (!response.ok) {
      let detail = '';
      try {
        const body = await response.json();
        detail = Array.isArray(body.detail)
          ? body.detail.map(item => item.msg).join(' · ')
          : body.detail || body.message || '';
      } catch (_) {
        try { detail = await response.text(); } catch (_) { /* empty response */ }
      }
      throw new Error(detail || `Ошибка запроса (${response.status})`);
    }
    if (response.status === 204) return null;
    const type = response.headers.get('content-type') || '';
    return type.includes('application/json') ? response.json() : response.text();
  }

  function showLogin(message = '') {
    document.querySelector('dialog[open]')?.close();
    integrationsList.replaceChildren();
    adminView.hidden = true;
    loginView.hidden = false;
    loginMessage.textContent = message;
    if (message) loginMessage.classList.add('error');
    else loginMessage.classList.remove('error');
  }

  function showAdmin() {
    loginView.hidden = true;
    adminView.hidden = false;
  }

  function showNotice(message, isError = false) {
    notice.textContent = message;
    notice.classList.toggle('error', isError);
    notice.hidden = !message;
  }

  function setActiveSection(name) {
    const aliases = { grants: 'bots', templates: 'bots' };
    const section = Object.hasOwn(aliases, name) ? aliases[name] : name;
    const selected = Object.hasOwn(sections, section) ? section : 'botx';
    if (selected !== activeSection) document.querySelector('dialog[open]')?.close();
    activeSection = selected;
    createButton.setAttribute('aria-controls', dialogs[selected].id);
    document.querySelectorAll('.side-link').forEach(button => {
      const active = button.dataset.section === selected;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    Object.entries(sections).forEach(([key, section]) => { section.element.hidden = key !== selected; });
    document.getElementById('page-title').textContent = sections[selected].title;
    document.getElementById('page-description').textContent = sections[selected].description;
    showNotice('');
  }

  function makeEmpty(title, description) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    const heading = document.createElement('strong');
    heading.textContent = title;
    const detail = document.createElement('span');
    detail.textContent = description;
    empty.append(heading, detail);
    return empty;
  }

  function renderBots() {
    botsList.replaceChildren();
    document.getElementById('bot-count').textContent = String(bots.length);
    document.getElementById('bots-badge').textContent = String(bots.length);
    if (!bots.length) botsList.append(makeEmpty('Подключений пока нет', 'Добавьте учётные данные BotX.'));
    for (const bot of bots) {
      const row = document.createElement('article');
      row.className = 'template-card record-card';
      const systems = integrations.filter(system => system.routes.some(route => route.bot_id === bot.bot_id));
      const handler = bot.internal_bot_id ? [{ name: bot.internal_bot_name, type: 'bot', edge: 'exchange' }] : [];
      const state = integrationText('span', bot.notification_ready ? 'Подключён' : 'Недоступен',
        `status-tag${bot.notification_ready ? '' : ' disabled'}`);
      const check = integrationButton('Проверить');
      check.setAttribute('aria-label', `Проверить подключение «${bot.name}»`);
      const checkResult = integrationText('p', '', 'notice botx-check-result');
      checkResult.setAttribute('role', 'status');
      checkResult.hidden = true;
      check.addEventListener('click', async () => {
        check.disabled = true;
        checkResult.hidden = false;
        checkResult.classList.remove('error');
        checkResult.textContent = 'Проверяем доступ к CTS…';
        try {
          const result = await api(`/api/v1/admin/botx/${bot.bot_id}/check`, { method: 'POST' });
          checkResult.textContent = `CTS доступен. Доступных чатов: ${result.chat_count}.`;
        } catch (error) {
          checkResult.classList.add('error');
          checkResult.textContent = error.message;
        } finally {
          check.disabled = false;
        }
      });
      row.append(
        recordHeading('botx', bot, check),
        recordBody([
          ['Статус', state],
          ['Bot ID', integrationText('span', bot.bot_id, 'mono')],
          ['CTS URL', bot.cts_url || 'Не указан'],
          ['Режим', handler.length ? 'Входящие и отправка' : 'Только отправка'],
        ], { name: bot.name, type: 'botx' },
        systems.map(system => ({ name: system.name, type: 'system', edge: 'send' })), handler),
        checkResult,
      );
      botsList.append(row);
    }
  }

  function fillTemplates(select, selected = 'echo') {
    select.replaceChildren(new Option('Выберите тип бота', ''));
    for (const template of templates) select.add(new Option(template.name, template.id));
    if (selected && !templates.some(template => template.id === selected) && selected !== 'echo') {
      select.add(new Option(`${selected} · недоступен`, selected));
    }
    select.value = Array.from(select.options).some(option => option.value === selected) ? selected : '';
  }

  function internalSettings(form) {
    const fields = new FormData(form);
    const botxId = fields.get('botx_id');
    return {
      name: fields.get('name').trim(),
      botx_id: botxId === 'unlinked' ? null : botxId,
      template_id: fields.get('template_id'),
      groups_enabled: fields.has('groups_enabled'),
      group_history_enabled: form.elements.group_history_enabled.checked,
    };
  }

  function setGroupHistoryState(form) {
    const groups = form.elements.groups_enabled;
    const history = form.elements.group_history_enabled;
    history.disabled = !groups.checked;
  }

  function renderInternalBots() {
    const list = document.getElementById('internal-bots-list');
    const expanded = new Set([...grantViews].filter(([, view]) => view.details.open).map(([id]) => id));
    grantViews.clear();
    list.replaceChildren();
    if (!internalBots.length) list.append(makeEmpty('Нет внутренних ботов', 'Создайте бота и выберите для него подключение BotX.'));
    for (const handler of internalBots) {
      const connection = bots.find(bot => bot.bot_id === handler.botx_id);
      const card = document.createElement('article');
      card.className = 'template-card record-card';
      const template = templates.find(item => item.id === handler.template_id);
      const grant = integrationButton('Выдать доступ');
      grant.setAttribute('aria-haspopup', 'dialog');
      grant.addEventListener('click', () => openGrantDialog(handler));
      card.append(
        recordHeading('bots', handler, grant),
        recordBody([
          ['Тип бота', template?.name || handler.template_id],
          ['Групповые чаты', handler.groups_enabled ? 'Включены' : 'Выключены'],
          ['История групп', handler.groups_enabled && handler.group_history_enabled ? 'Сохраняется' : 'Не сохраняется'],
        ], { name: handler.name, type: 'bot' },
        connection ? [{ name: connection.name, type: 'botx', edge: 'exchange' }] : []),
      );
      if (template?.commands.length) {
        const commandList = document.createElement('ul');
        commandList.className = 'command-list';
        for (const command of template.commands) {
          commandList.append(integrationText('li', `${command.name} · ${command.description}`));
        }
        card.append(recordDetails('Команды', commandList));
      }
      const entries = integrationText('div', '', 'data-list');
      entries.setAttribute('aria-live', 'polite');
      const details = recordDetails('Выданные доступы', entries);
      details.open = expanded.has(handler.id);
      const summary = details.firstElementChild;
      grantViews.set(handler.id, { details, summary, entries });
      card.append(details);
      list.append(card);
    }
    renderGrants();
  }

  function updateIdentityFields() {
    const type = document.getElementById('grant-identity-type').value;
    const input = document.getElementById('grant-identity');
    const label = document.getElementById('grant-identity-label');
    const domainWrap = document.getElementById('ad-domain-field');
    const domain = document.getElementById('grant-ad-domain');
    const settings = {
      email: { label: 'Email', placeholder: 'user@example.com', autocomplete: 'email', inputType: 'email' },
      ad: { label: 'AD логин', placeholder: 'username', autocomplete: 'username', inputType: 'text' },
      huid: { label: 'HUID', placeholder: 'UUID пользователя', autocomplete: 'off', inputType: 'text' },
    }[type];
    label.textContent = settings.label;
    input.placeholder = settings.placeholder;
    input.autocomplete = settings.autocomplete;
    input.type = settings.inputType;
    domainWrap.hidden = type !== 'ad';
    domain.required = type === 'ad';
  }

  function renderGrants() {
    const byBot = new Map();
    for (const grant of grants) {
      if (!byBot.has(grant.internal_bot_id)) byBot.set(grant.internal_bot_id, []);
      byBot.get(grant.internal_bot_id).push(grant);
    }
    for (const [id, view] of grantViews) {
      const botGrants = byBot.get(id) || [];
      view.summary.textContent = `Выданные доступы · ${botGrants.length}`;
      view.entries.replaceChildren(...botGrants.map(renderGrant));
      if (!botGrants.length) view.entries.append(integrationText('p', 'Доступы ещё не выданы.', 'muted'));
    }
  }

  function renderGrant(grant) {
    const row = integrationText('div', '', 'data-row');
    const details = integrationText('div', '', 'data-main');
    const user = grant.identity || grant.user_huid;
    const identityLabels = { email: 'Email', ad: 'AD аккаунт', huid: 'HUID' };
    const identityLabel = identityLabels[grant.identity_type] || 'Пользователь';
    const domain = grant.identity_type === 'ad' && grant.ad_domain ? ` · ${grant.ad_domain}` : '';
    details.append(integrationText('strong', user), integrationText('small', identityLabel + domain));
    const remove = integrationButton('Отозвать', 'remove-button');
    remove.setAttribute('aria-label', `Отозвать доступ для ${user}`);
    remove.addEventListener('click', () => {
      if (!window.confirm(`Отозвать доступ для «${user}»?`)) return;
      saveChanges(remove, async () => {
        await api(`/api/v1/admin/grants/${grant.id}`, { method: 'DELETE' });
        await loadGrants();
        grantViews.get(grant.internal_bot_id)?.summary.focus();
      }, 'Доступ отозван.');
    });
    row.append(details, remove);
    return row;
  }

  async function loadGrants() {
    grants = await api('/api/v1/admin/grants');
    renderGrants();
  }

  function integrationText(tag, value, className = '') {
    const element = document.createElement(tag);
    element.textContent = value;
    element.className = className;
    return element;
  }

  function integrationButton(label, className = 'button button-quiet') {
    const button = integrationText('button', label, className);
    button.type = 'button';
    return button;
  }

  function recordDetails(title, ...content) {
    const details = document.createElement('details');
    details.className = 'integration-log';
    details.append(integrationText('summary', title), ...content);
    return details;
  }

  function recordBody(attributes, center, left, right) {
    const body = integrationText('div', '', 'record-body');
    const list = integrationText('dl', '', 'record-attributes');
    for (const [label, value] of attributes) {
      const term = integrationText('dt', label);
      const description = integrationText('dd', '');
      description.append(value);
      list.append(term, description);
    }
    body.append(list, relationshipGraph(center, left, right));
    return body;
  }

  const graphTypes = { botx: 'BotX', bot: 'Внутренний бот', system: 'Внешняя система' };
  const graphRelations = { exchange: 'Входящие и ответы', send: 'Отправка из системы' };

  function svgElement(tag, attributes = {}) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
    return element;
  }

  function relationshipGraph(center, left = [], right = []) {
    const panel = integrationText('figure', '', 'relationship-graph');
    const nodeWidth = 126;
    const nodeHeight = 52;
    const spacing = 63;
    const gap = 40;
    const padding = 7;
    const columns = 1 + Number(left.length > 0) + Number(right.length > 0);
    const width = columns * nodeWidth + (columns - 1) * gap + padding * 2;
    const height = padding * 2 + nodeHeight + (Math.max(1, left.length, right.length) - 1) * spacing;
    const root = {
      ...center, x: padding + (left.length ? nodeWidth + gap : 0),
      y: Math.min(height / 2, 110),
    };
    const descriptions = [];
    const relationTypes = new Set();
    const svg = svgElement('svg', {
      width, height, viewBox: `0 0 ${width} ${height}`, role: 'img',
    });
    const edges = svgElement('g');
    const nodes = svgElement('g');
    svg.append(edges, nodes);

    function addNode(node, primary = false) {
      const y = node.y - nodeHeight / 2;
      const group = svgElement('g', { class: `graph-node graph-node-${node.type}${primary ? ' graph-node-primary' : ''}` });
      const title = svgElement('title');
      title.textContent = `${node.name} · ${graphTypes[node.type]}`;
      const label = svgElement('foreignObject', { x: node.x, y, width: nodeWidth, height: nodeHeight });
      const text = integrationText('div', '', 'graph-node-label');
      text.append(integrationText('strong', node.name), integrationText('small', graphTypes[node.type]));
      label.append(text);
      group.append(title, svgElement('rect', { x: node.x, y, width: nodeWidth, height: nodeHeight, rx: 8 }), label);
      nodes.append(group);
    }

    function addConnection(source, target, relation) {
      const x1 = source.x + nodeWidth + 2;
      const x2 = target.x - 2;
      const mid = (x1 + x2) / 2;
      const y1 = source.y;
      const y2 = target.y;
      const line = svgElement('path', { d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}` });
      const arrows = svgElement('path', {
        d: `M ${x2 - 6} ${y2 - 4} L ${x2} ${y2} L ${x2 - 6} ${y2 + 4}`
          + (relation === 'exchange' ? ` M ${x1 + 6} ${y1 - 4} L ${x1} ${y1} L ${x1 + 6} ${y1 + 4}` : ''),
      });
      const group = svgElement('g', { class: `graph-edge graph-edge-${relation}` });
      group.append(line, arrows);
      edges.append(group);
      relationTypes.add(relation);
      const direction = relation === 'exchange' ? '↔' : '→';
      descriptions.push(`${source.name} ${direction} ${target.name}: ${graphRelations[relation]}`);
    }

    for (const [isLeft, side] of [[true, left], [false, right]]) {
      const top = side.length > 1 ? (height - (side.length - 1) * spacing) / 2 : root.y;
      side.forEach((item, index) => {
        const node = { ...item, x: isLeft ? padding : root.x + nodeWidth + gap, y: top + index * spacing };
        addNode(node);
        addConnection(isLeft ? node : root, isLeft ? root : node, item.edge);
      });
    }
    addNode(root, true);

    svg.setAttribute('aria-label', descriptions.length
      ? descriptions.join('. ')
      : `${center.name}, ${graphTypes[center.type]}. Нет прямых связей.`);
    const scroller = integrationText('div', '', 'graph-scroll');
    if (columns === 3) scroller.classList.add('graph-scroll-wide');
    if (columns === 2) scroller.classList.add('graph-scroll-pair');
    scroller.append(svg);
    if (columns > 1 || height > 265) {
      scroller.setAttribute('role', 'region');
      scroller.tabIndex = 0;
      scroller.setAttribute('aria-label', 'Схема связей с прокруткой');
    }
    panel.append(scroller);
    const legend = integrationText('figcaption', '', 'graph-legend');
    for (const relation of relationTypes) {
      legend.append(integrationText('span', graphRelations[relation], `graph-key graph-key-${relation}`));
    }
    if (!relationTypes.size) legend.textContent = 'Нет прямых связей';
    panel.append(legend);
    return panel;
  }

  function formatDate(timestamp) {
    return new Date(timestamp).toLocaleString('ru-RU');
  }

  function deliveryLog(integrationId) {
    const entries = document.createElement('div');
    entries.className = 'data-list';
    entries.setAttribute('aria-live', 'polite');
    const refresh = integrationButton('Обновить журнал');
    const more = integrationButton('Показать ещё');
    more.hidden = true;
    let cursor = null;
    let loading = false;
    async function load(beforeId = null) {
      if (loading) return;
      loading = refresh.disabled = more.disabled = true;
      try {
        const query = beforeId ? `?before_id=${beforeId}` : '';
        const result = await api(`${integrationsUrl}/${integrationId}/logs${query}`);
        if (!beforeId) entries.replaceChildren();
        if (!result.entries.length && !beforeId) {
          entries.append(makeEmpty('Уведомлений пока нет', 'Здесь появятся уведомления, принятые по токенам этой интеграции.'));
        }
        for (const entry of result.entries) {
          const statuses = { pending: 'В очереди', processing: 'Отправляется', completed: 'Доставлено', failed: 'Ошибка доставки' };
          const row = integrationText('div', '', 'data-row data-main');
          row.append(
            integrationText('strong', `№${entry.job_id} · ${statuses[entry.status] || entry.status} · попыток: ${entry.attempts}`),
            integrationText('small', `${formatDate(entry.created_at)} · BotX ${entry.bot_name} · токен ${entry.token_prefix}…`),
            integrationText('small', `Чат ${entry.chat_id}`, 'mono'),
          );
          if (entry.delivery_error) {
            const error = entry.delivery_error === 'timeout' ? 'Истекло время ожидания BotX' : 'BotX не подтвердил отправку';
            row.append(integrationText('small', error));
          }
          entries.append(row);
        }
        cursor = result.next_before_id;
        more.hidden = !cursor;
      } catch (error) {
        showNotice(error.message, true);
      } finally {
        loading = refresh.disabled = more.disabled = false;
      }
    }
    refresh.addEventListener('click', () => load());
    more.addEventListener('click', () => load(cursor));
    const actions = integrationText('div', '', 'form-actions');
    actions.append(refresh, more);
    const details = recordDetails('Журнал доставки', actions, entries);
    details.addEventListener('toggle', () => { if (details.open) load(); });
    return details;
  }

  function renderIntegration(integration) {
    const card = document.createElement('article');
    card.className = 'template-card record-card';
    const tokenList = document.createElement('div');
    tokenList.className = 'data-list';
    const activeTokens = integration.tokens.filter(token => !token.revoked_at);
    for (const token of activeTokens) {
      const row = document.createElement('div');
      row.className = 'data-row';
      const info = document.createElement('div');
      info.className = 'data-main';
      info.append(
        integrationText('strong', `${token.prefix}…`),
        integrationText('small', token.bot_id
          ? `Прежний токен · только ${bots.find(bot => bot.bot_id === token.bot_id)?.name || token.bot_id}`
          : 'Для всех маршрутов системы'),
        integrationText('small', `Выдан ${formatDate(token.created_at)}`),
      );
      row.append(info);
      const revoke = integrationButton('Отозвать', 'remove-button');
      revoke.addEventListener('click', () => {
        if (!window.confirm(`Отозвать токен ${token.prefix}…?`)) return;
        saveChanges(revoke, async () => {
          await api(`${integrationsUrl}/${integration.id}/tokens/${token.id}`, { method: 'DELETE' });
          await loadTopology();
        }, 'Токен отозван.');
      });
      row.append(revoke);
      tokenList.append(row);
    }
    const issue = integrationButton('Выпустить токен');
    issue.id = `issue-token-${integration.id}`;
    issue.setAttribute('aria-haspopup', 'dialog');
    issue.addEventListener('click', () => openTokenDialog(integration));
    if (!activeTokens.length) tokenList.append(integrationText('p', 'Нет действующих токенов.', 'muted'));
    const credentials = recordDetails(`Токены системы · ${activeTokens.length}`, tokenList);
    credentials.id = `system-tokens-${integration.id}`;
    credentials.open = Boolean(document.getElementById(credentials.id)?.open);
    const connections = integration.routes.map(route => ({ name: route.bot_name, type: 'botx', edge: 'send' }));
    card.append(
      recordHeading('integrations', integration, issue),
      recordBody([
        ['Получатели и назначение', integrationText('span', integration.description, 'record-description')],
        ['Подключения BotX', String(connections.length)],
      ], { name: integration.name, type: 'system' }, [], connections),
      credentials, deliveryLog(integration.id),
    );
    return card;
  }

  function renderIntegrations() {
    integrationsList.replaceChildren(...integrations.map(renderIntegration));
    if (!integrations.length) {
      integrationsList.append(makeEmpty('Нет внешних систем', 'Добавьте систему, настройте маршруты и выпустите токен.'));
    }
  }

  async function loadTopology() {
    [bots, internalBots, templates, integrations] = await Promise.all([
      api('/api/v1/admin/botx'),
      api('/api/v1/admin/internal-bots'),
      api('/api/v1/admin/bot-templates'),
      api(integrationsUrl),
    ]);
    if (adminView.hidden) return;
    const templateSelect = document.getElementById('bot-template');
    fillTemplates(templateSelect, templateSelect.value || 'echo');
    renderBots();
    renderInternalBots();
    renderIntegrations();
  }

  async function saveChanges(button, action, message) {
    button.disabled = true;
    showNotice('');
    try {
      await action();
      showNotice(message);
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function loadAll() {
    showNotice('');
    const refreshButton = document.getElementById('refresh-button');
    refreshButton.disabled = true;
    try {
      await loadTopology();
      await loadGrants();
    } catch (error) {
      if (!loginView.hidden) return;
      showNotice(error.message || 'Не удалось загрузить данные.', true);
    } finally {
      refreshButton.disabled = false;
    }
  }

  loginForm.addEventListener('submit', async event => {
    event.preventDefault();
    const button = loginForm.querySelector('button[type="submit"]');
    const formData = new FormData(loginForm);
    button.disabled = true;
    loginMessage.textContent = '';
    try {
      await api('/api/v1/admin/login', {
        method: 'POST',
        body: JSON.stringify({ token: formData.get('token') }),
      });
      loginForm.reset();
      showAdmin();
      setActiveSection(location.hash.slice(1));
      await loadAll();
    } catch (error) {
      showLogin(error.message || 'Не удалось выполнить вход. Проверьте административный токен.');
    } finally {
      button.disabled = false;
    }
  });

  const dialogConfigs = {
    botx: {
      path: '/api/v1/admin/botx',
      id: record => record.bot_id,
      title: 'Подключить BotX',
      submit: 'Подключить',
      message: 'BotX подключён.',
      read(form, record) {
        const fields = Object.fromEntries(new FormData(form));
        const data = {
          name: fields.name.trim(),
          cts_url: fields.cts_url.trim() || null,
        };
        if (!record) return { ...data, bot_id: fields.bot_id.trim(), secret_key: fields.secret_key };
        return data;
      },
      fill(form, record) {
        form.elements.bot_id.value = record?.bot_id || '';
        form.elements.bot_id.readOnly = Boolean(record);
        form.elements.cts_url.value = record?.cts_url || '';
        form.elements.cts_url.required = !record || Boolean(record.cts_url);
        form.elements.secret_key.required = !record;
        form.elements.secret_key.disabled = Boolean(record);
        document.getElementById('bot-secret-field').hidden = Boolean(record);

      },
    },
    bots: {
      path: '/api/v1/admin/internal-bots',
      id: record => record.id,
      title: 'Новый внутренний бот',
      submit: 'Создать бота',
      message: 'Внутренний бот создан.',
      read: internalSettings,
      fill(form, record) {
        fillTemplates(form.elements.template_id, record?.template_id);
        const connection = form.elements.botx_id;
        const available = bots.filter(bot => !bot.internal_bot_id || bot.internal_bot_id === record?.id);
        connection.replaceChildren(new Option(available.length ? 'Выберите BotX' : 'Нет свободных подключений BotX', ''));
        if (record) connection.add(new Option('Без подключения', 'unlinked'));
        for (const bot of available) connection.add(new Option(bot.name, bot.bot_id));
        connection.value = record ? record.botx_id || 'unlinked' : '';
        form.elements.groups_enabled.checked = record?.groups_enabled ?? true;
        form.elements.group_history_enabled.checked = record?.group_history_enabled ?? true;
        setGroupHistoryState(form);
      },
    },
    integrations: {
      path: integrationsUrl,
      id: record => record.id,
      title: 'Новая внешняя система',
      submit: 'Добавить систему',
      message: 'Система добавлена. Теперь можно выпустить токен.',
      read: readIntegrationSettings,
      fill(form, record) {
        form.elements.description.value = record?.description || '';
        integrationRoutes.replaceChildren();
        for (const route of record ? record.routes : [null]) addRouteFields(route);
        updateAddRouteButton();
      },
    },
  };
  const recordEditors = {};

  function showSectionHelp(section) {
    const template = document.getElementById(`help-${section}`);
    helpContent.replaceChildren(template.content.cloneNode(true));
    document.getElementById('help-heading').textContent = `Справка · ${sections[section].title}`;

    if (section === 'integrations') {
      const system = helpContent.querySelector('[data-help-system]');
      const select = helpContent.querySelector('[data-help-bot]');
      const example = helpContent.querySelector('[data-help-send]');
      for (const item of integrations) system.add(new Option(item.name, item.id));
      helpContent.querySelector('[data-help-integration]').hidden = !integrations.length;

      function updateExample() {
        const data = { bot_id: select.value || 'BOTX_UUID', chat_id: 'CHAT_UUID', text: 'Проверка доставки' };
        example.textContent = [
          'curl -sS -X POST "$GATEWAY_URL/api/v1/messages"',
          '  -H "X-Notification-Token: $INTEGRATION_TOKEN"',
          '  -H "Content-Type: application/json"',
          `  --data '${JSON.stringify(data)}'`,
        ].join(' \\' + '\n');
      }

      function updateRoutes() {
        const routes = integrations.find(item => String(item.id) === system.value)?.routes || [];
        select.replaceChildren(...routes.map(route => new Option(route.bot_name, route.bot_id)));
        helpContent.querySelector('[data-help-route]').hidden = !routes.length;
        helpContent.querySelector('[data-help-no-routes]').hidden = Boolean(routes.length);
        updateExample();
      }

      system.addEventListener('change', updateRoutes);
      select.addEventListener('change', updateExample);
      updateRoutes();
    }

    helpDialog.showModal();
  }

  function recordHeading(section, record, extraAction) {
    const heading = integrationText('div', '', 'card-heading');
    const edit = integrationButton('Редактировать');
    edit.id = `edit-${section}-${dialogConfigs[section].id(record)}`;
    edit.setAttribute('aria-haspopup', 'dialog');
    edit.setAttribute('aria-label', `Редактировать «${record.name}»`);
    edit.addEventListener('click', () => recordEditors[section](record, edit.id));
    const actions = integrationText('div', '', 'card-actions');
    actions.append(edit);
    if (extraAction) actions.append(extraAction);
    heading.append(integrationText('h3', record.name), actions);
    return heading;
  }

  function bindFormDialog(dialog, save, reload) {
    const form = dialog.querySelector('form');
    const submit = form.querySelector('button[type="submit"]');
    const cancel = form.querySelector('[data-close-dialog]');
    const errorNotice = form.querySelector('.create-error');

    cancel.addEventListener('click', () => dialog.close());
    dialog.addEventListener('cancel', event => {
      if (submit.disabled) event.preventDefault();
    });
    dialog.addEventListener('close', () => {
      form.reset();
      errorNotice.hidden = true;
      errorNotice.textContent = '';
    });

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (submit.disabled) return;
      submit.disabled = cancel.disabled = true;
      errorNotice.hidden = true;
      let result;
      try {
        result = await save(form);
      } catch (error) {
        if (dialog.open) {
          errorNotice.textContent = error.message;
          errorNotice.hidden = false;
        }
        return;
      } finally {
        submit.disabled = cancel.disabled = false;
      }
      dialog.close();
      try {
        await reload();
        if (result.focusId && !document.querySelector('dialog[open]')) {
          document.getElementById(result.focusId)?.focus();
        }
        showNotice(result.message);
      } catch (error) {
        if (!adminView.hidden) showNotice(result.message + ' Список не обновился. ' + error.message, true);
      }
    });
  }

  function bindRecordDialog(section) {
    const dialog = dialogs[section];
    const config = dialogConfigs[section];
    const form = dialog.querySelector('form');
    let currentRecord = null;
    let focusId;
    bindFormDialog(dialog, async form => {
      const record = currentRecord;
      const target = record ? `${config.path}/${config.id(record)}` : config.path;
      const result = { message: record ? 'Изменения сохранены.' : config.message, focusId };
      await api(target, {
        method: record ? 'PUT' : 'POST', body: JSON.stringify(config.read(form, record)),
      });
      return result;
    }, async () => {
      await loadTopology();
      if (section === 'bots') await loadGrants();
    });
    return (record = null, editId) => {
      currentRecord = record;
      focusId = editId;
      form.reset();
      form.elements.name.value = record?.name || '';
      config.fill(form, record);
      dialog.querySelector('h2').textContent = record ? 'Редактирование · ' + record.name : config.title;
      dialog.querySelector('.panel-heading p').hidden = Boolean(record);
      form.querySelector('button[type="submit"]').textContent = record ? 'Сохранить' : config.submit;
      dialog.showModal();
      form.elements.name.focus();
    };
  }

  const internalForm = document.getElementById('internal-bot-form');
  internalForm.elements.groups_enabled.addEventListener('change', () => setGroupHistoryState(internalForm));
  for (const section of Object.keys(dialogs)) recordEditors[section] = bindRecordDialog(section);
  createButton.addEventListener('click', () => recordEditors[activeSection]());
  document.querySelectorAll('[data-help-section]').forEach(button => {
    button.addEventListener('click', () => showSectionHelp(button.dataset.helpSection));
  });

  window.addEventListener('hashchange', () => setActiveSection(location.hash.slice(1)));
  document.querySelectorAll('.side-link').forEach(button => {
    button.addEventListener('click', () => { location.hash = button.dataset.section; });
  });
  const integrationRoutes = document.getElementById('integration-routes');
  const addRouteButton = document.getElementById('add-integration-route');

  function updateAddRouteButton() {
    addRouteButton.disabled = integrationRoutes.childElementCount >= Math.min(bots.length, 100);
  }

  function addRouteFields(route = null) {
    const row = document.createElement('div');
    row.className = 'route-fields';
    row.innerHTML = `
      <label class="field-label">Подключение BotX<select required></select></label>
      <button class="remove-button" type="button">Убрать маршрут</button>`;
    const select = row.querySelector('select');
    select.add(new Option(bots.length ? 'Выберите BotX' : 'Сначала подключите BotX', ''));
    for (const bot of bots) select.add(new Option(bot.name, bot.bot_id));
    select.value = route?.bot_id || '';
    row.querySelector('button').addEventListener('click', () => {
      const next = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      updateAddRouteButton();
      (next?.querySelector('select') || addRouteButton).focus();
    });
    integrationRoutes.append(row);
    updateAddRouteButton();
    return select;
  }

  addRouteButton.addEventListener('click', () => addRouteFields().focus());

  function readIntegrationSettings(form, record) {
    const routes = [];
    const botIds = new Set();
    for (const row of integrationRoutes.children) {
      const botId = row.querySelector('select').value;
      if (botIds.has(botId)) throw new Error('Каждый BotX можно выбрать только один раз.');
      botIds.add(botId);
      routes.push({ bot_id: botId });
    }
    if (!record && !routes.length) throw new Error('Добавьте хотя бы один маршрут через BotX.');
    return { ...Object.fromEntries(new FormData(form)), routes };
  }

  const tokenDialog = document.getElementById('token-dialog');
  const tokenResult = document.getElementById('issued-token-result');
  const closeTokenButton = document.getElementById('close-token-dialog');
  const confirmTokenButton = document.getElementById('confirm-issue-token');
  const copyTokenButton = document.getElementById('copy-issued-token');
  const tokenMessage = document.getElementById('token-dialog-message');
  let tokenTarget;

  function showTokenMessage(message, error = false) {
    tokenMessage.textContent = message;
    tokenMessage.classList.toggle('error', error);
    tokenMessage.hidden = !message;
  }

  function openTokenDialog(system) {
    if (confirmTokenButton.disabled) return;
    tokenTarget = system;
    document.getElementById('token-system-name').textContent = system.name;
    issuedToken.value = '';
    tokenResult.hidden = copyTokenButton.hidden = true;
    confirmTokenButton.hidden = false;
    closeTokenButton.textContent = 'Отмена';
    showTokenMessage('');
    tokenDialog.showModal();
  }

  closeTokenButton.addEventListener('click', () => tokenDialog.close());
  tokenDialog.addEventListener('cancel', event => {
    if (confirmTokenButton.disabled) event.preventDefault();
  });
  tokenDialog.addEventListener('close', () => {
    issuedToken.value = '';
    tokenResult.hidden = true;
    if (activeSection === 'integrations' && !adminView.hidden) {
      document.getElementById(`issue-token-${tokenTarget?.id}`)?.focus();
    }
  });
  confirmTokenButton.addEventListener('click', async () => {
    const system = tokenTarget;
    confirmTokenButton.disabled = closeTokenButton.disabled = true;
    showTokenMessage('');
    try {
      const result = await api(`${integrationsUrl}/${system.id}/tokens`, { method: 'POST' });
      if (tokenDialog.open && tokenTarget === system) {
        issuedToken.value = result.token;
        tokenResult.hidden = copyTokenButton.hidden = false;
        confirmTokenButton.hidden = true;
        closeTokenButton.textContent = 'Готово';
        issuedToken.focus();
        issuedToken.select();
      }
    } catch (error) {
      if (tokenDialog.open) showTokenMessage(error.message, true);
      return;
    } finally {
      confirmTokenButton.disabled = closeTokenButton.disabled = false;
    }
    try {
      await loadTopology();
    } catch (error) {
      const message = 'Токен выпущен. Список не обновился. ' + error.message;
      if (tokenDialog.open && tokenTarget === system) showTokenMessage(message, true);
      else if (!adminView.hidden) showNotice(message, true);
    }
  });
  copyTokenButton.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(issuedToken.value);
      if (tokenDialog.open) showTokenMessage('Токен скопирован.');
    } catch (_) {
      if (!tokenDialog.open) return;
      issuedToken.focus();
      issuedToken.select();
      showTokenMessage('Скопируйте выделенный токен вручную.');
    }
  });

  const grantDialog = document.getElementById('grant-dialog');
  let grantBotId;
  function openGrantDialog(handler) {
    grantBotId = handler.id;
    document.getElementById('grant-dialog-heading').textContent = `Выдать доступ · ${handler.name}`;
    updateIdentityFields();
    grantDialog.showModal();
  }
  bindFormDialog(grantDialog, async form => {
    const fields = new FormData(form);
    const botId = grantBotId;
    const grant = {
      internal_bot_id: botId,
      identity_type: fields.get('identity_type'),
      identity: fields.get('identity').trim(),
      permission: 'use',
    };
    if (grant.identity_type === 'ad') grant.ad_domain = fields.get('ad_domain').trim();
    await api('/api/v1/admin/grants', { method: 'POST', body: JSON.stringify(grant) });
    const view = grantViews.get(botId);
    if (view) view.details.open = true;
    return { message: 'Доступ выдан.' };
  }, loadGrants);
  document.getElementById('grant-identity-type').addEventListener('change', updateIdentityFields);
  document.getElementById('refresh-button').addEventListener('click', loadAll);
  document.getElementById('logout-button').addEventListener('click', async event => {
    event.currentTarget.disabled = true;
    try { await api('/api/v1/admin/logout', { method: 'POST' }); }
    catch (_) { /* credentials still leave the page even if the session expired */ }
    showLogin('');
    document.getElementById('admin-token').focus();
    event.currentTarget.disabled = false;
  });

  updateIdentityFields();

  (async () => {
    try {
      await api('/api/v1/admin/session');
      showAdmin();
      setActiveSection(location.hash.slice(1));
      await loadAll();
    } catch (_) {
      if (loginView.hidden) showLogin('');
    }
  })();
})();

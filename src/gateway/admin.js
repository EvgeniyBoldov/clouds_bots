(() => {
  const loginView = document.getElementById('login-view');
  const adminView = document.getElementById('admin-view');
  const loginForm = document.getElementById('login-form');
  const loginMessage = document.getElementById('login-message');
  const botsList = document.getElementById('bots-list');
  const grantsList = document.getElementById('grants-list');
  const notice = document.getElementById('notice');
  const sections = {
    bots: {
      element: document.getElementById('bots-section'),
      title: 'Боты',
      description: 'Подключённые боты и их конфигурация.',
    },
    templates: {
      element: document.getElementById('templates-section'),
      title: 'Типы ботов',
      description: 'Шаблоны, зарегистрированные в текущем развёртывании.',
    },
    grants: {
      element: document.getElementById('grants-section'),
      title: 'Доступы',
      description: 'Управление разрешениями пользователей для персональных ботов.',
    },
  };
  let bots = [];
  let templates = [];

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
        detail = body.detail || body.message || '';
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
    const selected = sections[name] ? name : 'bots';
    document.querySelectorAll('.side-link').forEach(button => {
      const active = button.dataset.section === selected;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    Object.entries(sections).forEach(([key, section]) => { section.element.hidden = key !== selected; });
    document.getElementById('page-title').textContent = sections[selected].title;
    document.getElementById('page-description').textContent = sections[selected].description;
    document.getElementById('page-footer').hidden = selected === 'templates';
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
    if (!bots.length) {
      botsList.append(makeEmpty('Боты не настроены', 'Добавьте учётные данные бота в конфигурацию сервиса.'));
    }
    const select = document.getElementById('grant-bot');
    const previous = select.value;
    select.replaceChildren(new Option('Выберите бота', ''));
    for (const [index, bot] of bots.entries()) {
      const name = bot.name || bot.bot_id;
      const row = document.createElement('article');
      row.className = 'data-row bot-row';
      const details = document.createElement('div');
      details.className = 'data-main';
      const label = document.createElement('strong');
      label.textContent = name;
      const id = document.createElement('small');
      id.className = 'mono';
      id.textContent = bot.bot_id;
      const config = document.createElement('small');
      config.textContent = `Тип: ${templateNameFor(bot)}`;
      const cts = document.createElement('small');
      cts.textContent = `CTS: ${bot.cts_url || 'не указан'}`;
      const templateState = document.createElement('small');
      const templateUnavailable = bot.template_status === 'unavailable'
        || !templates.some(template => template.id === (bot.template_id || 'echo'));
      templateState.className = templateUnavailable ? 'template-unavailable' : 'template-available';
      templateState.textContent = templateUnavailable
        ? 'Тип недоступен в текущем развёртывании'
        : `Обработчик: ${bot.default_handler || 'не задан'}`;
      details.append(label, id, config, cts, templateState);
      const state = document.createElement('span');
      state.className = `status-tag${bot.enabled ? '' : ' disabled'}`;
      state.textContent = bot.enabled ? 'Активен' : 'Отключён';
      const binding = document.createElement('div');
      binding.className = 'bot-template-control';
      const bindingSelect = document.createElement('select');
      bindingSelect.id = `bot-template-${index}`;
      bindingSelect.setAttribute('aria-label', `Тип бота для ${name}`);
      const currentTemplateId = bot.template_id || 'echo';
      let currentFound = false;
      for (const template of templates) {
        const option = new Option(template.name || template.id, template.id);
        if (template.id === currentTemplateId) {
          option.selected = true;
          currentFound = true;
        }
        bindingSelect.add(option);
      }
      if (!currentFound) {
        const unavailable = new Option(`${currentTemplateId} · недоступен`, currentTemplateId, true, true);
        unavailable.disabled = true;
        bindingSelect.add(unavailable);
      }
      const changeButton = document.createElement('button');
      changeButton.className = 'button button-quiet template-change-button';
      changeButton.type = 'button';
      changeButton.textContent = 'Сменить';
      changeButton.disabled = !templates.length || bindingSelect.value === currentTemplateId;
      bindingSelect.disabled = !templates.length;
      bindingSelect.addEventListener('change', () => {
        changeButton.disabled = !templates.length || bindingSelect.value === currentTemplateId;
      });
      changeButton.addEventListener('click', async () => {
        const selectedTemplateId = bindingSelect.value;
        changeButton.disabled = true;
        bindingSelect.disabled = true;
        try {
          const updated = await api(`/api/v1/admin/bots/${encodeURIComponent(bot.bot_id)}`, {
            method: 'PATCH',
            body: JSON.stringify({ template_id: selectedTemplateId }),
          });
          bots = bots.map(item => item.bot_id === updated.bot_id ? updated : item);
          renderBots();
          showNotice(`Тип бота «${templateNameFor(updated)}» применён.`);
        } catch (error) {
          showNotice(error.message || 'Не удалось сменить тип бота.', true);
          bindingSelect.disabled = false;
          changeButton.disabled = bindingSelect.value === currentTemplateId;
        }
      });
      binding.append(bindingSelect, changeButton);
      row.append(details, state, binding);
      botsList.append(row);
      const option = new Option(`${name} · ${bot.bot_id}`, bot.bot_id);
      if (!bot.enabled) option.disabled = true;
      select.add(option);
    }
    if (bots.some(bot => bot.bot_id === previous && bot.enabled)) select.value = previous;
    renderTemplates();
  }

  function templateNameFor(bot) {
    const templateId = bot.template_id || 'echo';
    const template = templates.find(item => item.id === templateId);
    return template ? template.name : `${templateId} · недоступен`;
  }

  function populateTemplateSelect() {
    const select = document.getElementById('bot-template');
    const previous = select.value;
    select.replaceChildren();
    if (!templates.length) {
      select.add(new Option('Нет зарегистрированных типов', ''));
      select.disabled = true;
      document.querySelector('#bot-form button[type="submit"]').disabled = true;
      return;
    }
    select.add(new Option('Выберите тип бота', ''));
    for (const template of templates) {
      select.add(new Option(template.name || template.id, template.id));
    }
    select.disabled = false;
    document.querySelector('#bot-form button[type="submit"]').disabled = false;
    if (templates.some(template => template.id === previous)) {
      select.value = previous;
    } else {
      const echo = templates.find(template => template.id === 'echo');
      select.value = echo?.id || '';
    }
  }

  function renderTemplates() {
    const list = document.getElementById('templates-list');
    list.replaceChildren();
    document.getElementById('template-count').textContent = String(templates.length);
    document.getElementById('templates-badge').textContent = String(templates.length);
    if (!templates.length) {
      list.append(makeEmpty('Нет доступных типов', 'В текущем развёртывании не зарегистрированы шаблоны ботов.'));
      return;
    }
    for (const template of templates) {
      const card = document.createElement('article');
      card.className = 'template-card';
      const heading = document.createElement('div');
      heading.className = 'template-card-heading';
      const titleGroup = document.createElement('div');
      titleGroup.className = 'template-title-group';
      const title = document.createElement('h3');
      title.textContent = template.name || template.id;
      const identifier = document.createElement('small');
      identifier.className = 'mono';
      identifier.textContent = template.id;
      titleGroup.append(title, identifier);
      const linkedBots = bots.filter(bot => bot.template_id === template.id).length;
      const count = document.createElement('span');
      count.className = 'count-badge';
      count.textContent = `${linkedBots} BotX`;
      heading.append(titleGroup, count);
      const description = document.createElement('p');
      description.className = 'template-description';
      description.textContent = template.description || 'Описание не задано.';
      const commandsHeading = document.createElement('div');
      commandsHeading.className = 'command-list-heading';
      commandsHeading.textContent = 'Команды';
      const commands = document.createElement('ul');
      commands.className = 'command-list';
      if (!template.commands?.length) {
        const empty = document.createElement('li');
        empty.className = 'command-empty';
        empty.textContent = 'Команды не объявлены';
        commands.append(empty);
      } else {
        for (const command of template.commands) {
          const item = document.createElement('li');
          const commandName = document.createElement('strong');
          commandName.textContent = command.name;
          const commandDescription = document.createElement('span');
          commandDescription.textContent = command.description || '';
          item.append(commandName, commandDescription);
          commands.append(item);
        }
      }
      card.append(heading, description, commandsHeading, commands);
      list.append(card);
    }
  }

  async function loadTemplates() {
    templates = await api('/api/v1/admin/bot-templates');
    populateTemplateSelect();
    renderTemplates();
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

  async function loadBots() {
    bots = await api('/api/v1/admin/bots');
    renderBots();
  }

  async function loadGrants() {
    const grants = await api('/api/v1/admin/grants');
    grantsList.replaceChildren();
    document.getElementById('grant-count').textContent = String(grants.length);
    document.getElementById('grants-badge').textContent = String(grants.length);
    if (!grants.length) {
      grantsList.append(makeEmpty('Пока нет доступов', 'Выданные разрешения появятся здесь.'));
      return;
    }
    for (const grant of grants) {
      const row = document.createElement('article');
      row.className = 'data-row';
      const details = document.createElement('div');
      details.className = 'data-main';
      const user = document.createElement('strong');
      user.textContent = grant.identity || grant.user_id || grant.user_huid;
      const bot = bots.find(item => item.bot_id === grant.bot_id);
      const scope = document.createElement('small');
      const identityLabels = { email: 'Email', ad: 'AD аккаунт', huid: 'HUID' };
      const identityLabel = identityLabels[grant.identity_type] || 'Пользователь';
      const domainSuffix = grant.identity_type === 'ad' && grant.ad_domain ? ` · ${grant.ad_domain}` : '';
      scope.textContent = `${bot?.name || 'Бот'} · ${identityLabel}${domainSuffix} · ${grant.permission}`;
      details.append(user, scope);
      const remove = document.createElement('button');
      remove.className = 'remove-button';
      remove.type = 'button';
      remove.textContent = 'Удалить';
      remove.setAttribute('aria-label', `Удалить разрешение для ${grant.identity || grant.user_id || grant.user_huid}`);
      remove.addEventListener('click', async () => {
        if (!grant.id) {
          showNotice('Сервис не передал идентификатор разрешения. Обновите страницу и повторите попытку.', true);
          return;
        }
        remove.disabled = true;
        try {
          await api(`/api/v1/admin/grants/${encodeURIComponent(grant.id)}`, { method: 'DELETE' });
          showNotice('Доступ удалён.');
          await loadGrants();
        } catch (error) {
          showNotice(error.message, true);
          remove.disabled = false;
        }
      });
      row.append(details, remove);
      grantsList.append(row);
    }
  }

  async function loadAll() {
    showNotice('');
    const refreshButton = document.getElementById('refresh-button');
    refreshButton.disabled = true;
    try {
      await loadTemplates();
      await loadBots();
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
      setActiveSection('bots');
      await loadAll();
    } catch (error) {
      showLogin(error.message || 'Не удалось выполнить вход. Проверьте административный токен.');
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById('bot-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const fields = new FormData(form);
    submit.disabled = true;
    showNotice('');
    try {
      const templateId = fields.get('template_id');
      await api('/api/v1/admin/bots', {
        method: 'POST',
        body: JSON.stringify({
          bot_id: fields.get('bot_id').trim(),
          cts_url: fields.get('cts_url').trim(),
          secret_key: fields.get('secret_key'),
          name: fields.get('name').trim(),
          ...(templateId ? { template_id: templateId } : {}),
        }),
      });
      form.reset();
      showNotice('Бот подключён. Секретный ключ не возвращается в список и не сохраняется в браузере.');
      await loadBots();
    } catch (error) {
      showNotice(error.message || 'Не удалось подключить бота.', true);
    } finally {
      submit.disabled = false;
    }
  });

  document.querySelectorAll('.side-link').forEach(button => {
    button.addEventListener('click', () => setActiveSection(button.dataset.section));
  });
  document.getElementById('grant-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    const fields = new FormData(form);
    submit.disabled = true;
    showNotice('');
    try {
      const identityType = fields.get('identity_type');
      const grant = {
        bot_id: fields.get('bot_id'),
        identity_type: identityType,
        identity: fields.get('identity').trim(),
        permission: 'use',
      };
      if (identityType === 'ad') grant.ad_domain = fields.get('ad_domain').trim();
      await api('/api/v1/admin/grants', {
        method: 'POST',
        body: JSON.stringify(grant),
      });
      form.reset();
      updateIdentityFields();
      showNotice('Доступ выдан.');
      await loadGrants();
    } catch (error) {
      showNotice(error.message || 'Не удалось выдать доступ.', true);
    } finally {
      submit.disabled = false;
    }
  });
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
      await loadAll();
    } catch (_) {
      if (loginView.hidden) showLogin('');
    }
  })();
})();

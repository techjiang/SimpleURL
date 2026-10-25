/* 管理后台逻辑 */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const escapeHtml = (str) =>
    String(str == null ? '' : str).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let toastTimer = null;
  function toast(msg) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
  }

  function setStatus(id, msg, kind) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  // 写请求统一带 CSRF 令牌（后端双提交校验，见 app/core/middleware.py）
  function withCsrf(options) {
    const meta = document.querySelector('meta[name="csrf-token"]');
    const token = meta ? meta.content : '';
    const headers = Object.assign({}, options && options.headers);
    if (token) headers['X-CSRF-Token'] = token;
    return Object.assign({}, options, { headers });
  }

  async function api(path, options) {
    const res = await fetch(path, withCsrf(options));
    if (res.status === 401) { showLogin(); throw new Error('请先登录'); }
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(data.message || `请求失败（${res.status}）`);
    return data;
  }

  const post = (path, body) =>
    api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

  /* ---------- 登录 ---------- */
  function showLogin() { const g = $('#admin-login'); if (g) g.hidden = false; }
  function hideLogin() { const g = $('#admin-login'); if (g) g.hidden = true; }

  function bindLogin() {
    const form = $('#admin-login-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      setStatus('#login-status', '登录中…');
      try {
        await post('/api/admin/login', {
          username: ($('#login-user').value || '').trim(),
          password: $('#login-pass').value || '',
        });
        hideLogin();
        toast('登录成功');
        boot();
      } catch (err) {
        setStatus('#login-status', err.message, 'err');
      }
    });
    const out = $('#logout-btn');
    if (out) out.addEventListener('click', async () => {
      await post('/api/admin/logout', {});
      toast('已退出');
      showLogin();
    });
  }

  /* ---------- 页签 ---------- */
  function bindTabs() {
    const wrap = $('#admin-tabs');
    if (!wrap) return;
    wrap.addEventListener('click', (e) => {
      const btn = e.target.closest('.admin-tab');
      if (!btn) return;
      wrap.querySelectorAll('.admin-tab').forEach((b) => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.admin-panel').forEach((p) => {
        p.classList.toggle('active', p.dataset.panel === btn.dataset.tab);
      });
      if (btn.dataset.tab === 'dash') loadDashboard();
      if (btn.dataset.tab === 'links') loadAdminLinks();
      if (btn.dataset.tab === 'keys') loadKeys();
      if (btn.dataset.tab === 'guard') loadGuard();
      if (btn.dataset.tab === 'audit') loadAudit();
      if (btn.dataset.tab === 'settings') loadSettings();
    });
  }

  /* ---------- 仪表盘 ---------- */
  async function loadDashboard() {
    try {
      const d = await api('/api/admin/dashboard');
      const cards = [
        ['链接总数', d.total_links], ['总点击', d.total_clicks],
        ['已启用', d.enabled_links], ['已停用', d.disabled_links],
        ['已过期', d.expired_links], ['今日点击', d.clicks_today],
        ['拦截访问', d.blocked_events], ['待结算', d.pending_clicks],
      ];
      $('#dash-cards').innerHTML = cards
        .map(([k, v]) => `<div class="stat-card"><span class="stat-label">${k}</span><strong>${v}</strong></div>`)
        .join('');
      $('#dash-pending').textContent = d.pending_clicks;
      renderChart($('#dash-chart'), d.daily);
      $('#dash-top').innerHTML = d.top_links.length
        ? d.top_links.map((l) => `<li><span class="mono">/${escapeHtml(l.code)}</span><b>${l.clicks}</b></li>`).join('')
        : '<li><span>暂无数据</span><b>0</b></li>';
      $('#dash-recent').innerHTML = d.recent_clicks.length
        ? d.recent_clicks.map((c) => {
            const mark = c.blocked ? '<span class="badge exp">已拦截</span>' : '';
            // 带上来源平台，一眼看出这条是被微信拦的还是 QQ 里点的
            const plat = c.platform
              ? `<i class="plat-dot" data-plat="${escapeHtml(c.platform)}" title="${escapeHtml(c.platform_label || '')}"></i>`
              : '';
            const dev = c.device === 'mobile' ? '移动端' : c.device === 'desktop' ? '桌面端' : c.device === 'bot' ? '机器人' : '未知';
            const platName = c.platform ? `<span class="hint">${escapeHtml(c.platform_label || c.platform)}</span>` : '';
            return `<li><span class="mono">/${escapeHtml(c.code)}</span><span class="recent-meta">${platName}${plat}<span>${dev}</span>${mark}</span></li>`;
          }).join('')
        : '<li><span>暂无访问</span><b>—</b></li>';
    } catch (err) { toast(err.message); }
  }

  function renderChart(el, daily) {
    if (!el) return;
    const max = Math.max(1, ...daily.map((d) => d.count));
    el.innerHTML = daily
      .map((d) => `<div class="bar" title="${d.date}：${d.count}"><i style="height:${(d.count / max) * 100}%"></i><b>${d.date.slice(5)}</b></div>`)
      .join('');
  }

  /* ---------- 链接管理 ---------- */
  const lstate = { page: 1, size: 10, q: '', status: '', total: 0 };
  // 跨页保留勾选：翻页回来还得记得刚才选了谁
  const picked = new Set();

  function syncBulkBar() {
    const bar = $('#admin-bulk');
    if (!bar) return;
    bar.hidden = picked.size === 0;
    $('#admin-bulk-count').textContent = `已选 ${picked.size} 条`;
  }

  async function loadAdminLinks() {
    const params = new URLSearchParams({ page: lstate.page, page_size: lstate.size });
    if (lstate.q) params.set('q', lstate.q);
    if (lstate.status) params.set('status', lstate.status);
    try {
      const data = await api('/api/links?' + params.toString());
      lstate.total = data.total;
      $('#admin-links-body').innerHTML = data.items.map((l) => {
        const on = picked.has(l.code);
        return `<tr>
        <td class="col-check"><input type="checkbox" data-pick="${escapeHtml(l.code)}"${on ? ' checked' : ''} aria-label="选择 /${escapeHtml(l.code)}" /></td>
        <td><a class="code-cell" href="/s/${encodeURIComponent(l.code)}" target="_blank" rel="noopener">/${escapeHtml(l.code)}</a>${l.title ? `<span class="title-cell">${escapeHtml(l.title)}</span>` : ''}</td>
        <td class="target-cell" title="${escapeHtml(l.target)}">${escapeHtml(l.target)}</td>
        <td class="clicks-cell">${l.clicks}</td>
        <td>${l.expired ? '<span class="badge exp">已过期</span>' : (l.enabled ? '<span class="badge on">已启用</span>' : '<span class="badge off">已停用</span>')}</td>
        <td>
          <button class="btn tiny ghost" data-act="toggle" data-code="${escapeHtml(l.code)}">${l.enabled ? '停用' : '启用'}</button>
          <button class="btn tiny danger" data-act="del" data-code="${escapeHtml(l.code)}">删除</button>
        </td>
      </tr>`;
      }).join('');
      $('#admin-links-empty').hidden = data.items.length > 0;
      const pages = Math.max(1, Math.ceil(data.total / lstate.size));
      $('#admin-page-info').textContent = `第 ${data.page} 页 / 共 ${pages} 页`;
      $('#admin-prev').disabled = data.page <= 1;
      $('#admin-next').disabled = data.page * lstate.size >= data.total;
      const all = data.items.length > 0 && data.items.every((l) => picked.has(l.code));
      const box = $('#admin-check-all');
      if (box) { box.checked = all; box.indeterminate = !all && data.items.some((l) => picked.has(l.code)); }
      syncBulkBar();
    } catch (err) { toast(err.message); }
  }

  async function runBulk(action) {
    if (!picked.size) return;
    const label = { enable: '启用', disable: '停用', delete: '删除' }[action];
    if (action === 'delete' && !confirm(`确定删除选中的 ${picked.size} 条链接？该操作不可恢复`)) return;
    try {
      const r = await post('/api/links/bulk', { action, codes: [...picked] });
      toast(r.message || `已${label} ${r.affected} 条`);
      picked.clear();
      loadAdminLinks();
    } catch (err) { toast(err.message); }
  }

  function bindAdminLinks() {
    const body = $('#admin-links-body');
    if (!body) return;
    let t = null;
    $('#admin-search').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { lstate.q = e.target.value.trim(); lstate.page = 1; loadAdminLinks(); }, 260);
    });
    $('#admin-status').addEventListener('change', (e) => { lstate.status = e.target.value; lstate.page = 1; loadAdminLinks(); });
    $('#admin-prev').addEventListener('click', () => { if (lstate.page > 1) { lstate.page -= 1; loadAdminLinks(); } });
    $('#admin-next').addEventListener('click', () => { lstate.page += 1; loadAdminLinks(); });

    body.addEventListener('change', (e) => {
      const box = e.target.closest('input[data-pick]');
      if (!box) return;
      if (box.checked) picked.add(box.dataset.pick); else picked.delete(box.dataset.pick);
      const all = $('#admin-check-all');
      if (all) all.indeterminate = true;
      syncBulkBar();
    });
    const allBox = $('#admin-check-all');
    if (allBox) allBox.addEventListener('change', () => {
      body.querySelectorAll('input[data-pick]').forEach((box) => {
        box.checked = allBox.checked;
        if (allBox.checked) picked.add(box.dataset.pick); else picked.delete(box.dataset.pick);
      });
      allBox.indeterminate = false;
      syncBulkBar();
    });
    [['#bulk-enable', 'enable'], ['#bulk-disable', 'disable'], ['#bulk-delete', 'delete']].forEach(([sel, act]) => {
      const btn = $(sel);
      if (btn) btn.addEventListener('click', () => runBulk(act));
    });
    const clearBtn = $('#bulk-clear');
    if (clearBtn) clearBtn.addEventListener('click', () => { picked.clear(); loadAdminLinks(); });

    body.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const code = btn.dataset.code;
      try {
        if (btn.dataset.act === 'del') {
          if (!confirm(`确定删除 /${code}？不可恢复`)) return;
          await api('/api/links/' + encodeURIComponent(code), { method: 'DELETE' });
          picked.delete(code);
          toast(`已删除 /${code}`);
        } else {
          const cur = await api('/api/links/' + encodeURIComponent(code));
          await api('/api/links/' + encodeURIComponent(code), {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !cur.enabled }),
          });
          toast(`/${code} 已${cur.enabled ? '停用' : '启用'}`);
        }
        loadAdminLinks();
      } catch (err) { toast(err.message); }
    });
  }

  /* ---------- 操作留痕 ---------- */
  const ACTION_TONE = { create: 'on', delete: 'exp', update: '', settings: '', import: '', login: '', logout: '' };

  async function loadAudit() {
    const list = $('#audit-list');
    if (!list) return;
    const sel = $('#audit-action');
    try {
      const params = new URLSearchParams({ limit: 40 });
      if (sel && sel.value) params.set('action', sel.value);
      const data = await api('/api/admin/audit?' + params.toString());
      if (sel && sel.options.length <= 1) {
        data.actions.forEach((a) => {
          const opt = document.createElement('option');
          opt.value = a.key;
          opt.textContent = a.label;
          sel.appendChild(opt);
        });
      }
      const items = data.items || [];
      if (!items.length) {
        list.innerHTML = '<li><span class="hint">还没有操作记录</span><b>—</b></li>';
        return;
      }
      list.innerHTML = items.map((i) => {
        const where = i.code ? ` <span class="mono">/${escapeHtml(i.code)}</span>` : '';
        const what = i.detail ? `<span class="hint">${escapeHtml(i.detail)}</span>` : '';
        return `<li><span><span class="badge ${ACTION_TONE[i.action] || ''}">${escapeHtml(i.action_label)}</span>${where} ${what}</span>
          <b>${escapeHtml(i.actor)} · ${escapeHtml(shortTime(i.ts))}</b></li>`;
      }).join('');
    } catch (err) { toast(err.message); }
  }

  /* ---------- 防洪 / UA ---------- */
  async function loadGuard() {
    try {
      const s = await api('/api/admin/settings');
      $('#guard-redirect').value = s.redirect_rate_limit;
      $('#guard-click').value = s.click_daily_limit;
      const ua = await api('/api/admin/ua-rules');
      $('#ua-rules').value = (ua.rules || []).join('\n');
    } catch (err) { toast(err.message); }
    loadSocial();
  }

  /* ---------- 社交平台防洪 ---------- */
  const STRATEGY_LABELS = [
    ['allow', '放行并计数'],
    ['silent', '放行不计数'],
    ['block', '拦截并提示'],
  ];

  async function loadSocial() {
    try {
      const [rules, settings] = await Promise.all([
        api('/api/admin/social-rules'),
        api('/api/admin/settings'),
      ]);
      $('#social-enabled').checked = !!settings.social_guard;
      $('#social-hint').value = rules.hint || settings.social_block_hint || '';
      $('#social-grid').innerHTML = (rules.platforms || []).map((p) => `
        <label class="social-row">
          <span class="social-name"><i class="plat-dot" data-plat="${escapeHtml(p.key)}"></i>${escapeHtml(p.label)}</span>
          <select data-plat="${escapeHtml(p.key)}">
            ${STRATEGY_LABELS.map(([v, t]) =>
              `<option value="${v}"${p.strategy === v ? ' selected' : ''}>${t}</option>`).join('')}
          </select>
        </label>`).join('');
    } catch (err) { toast(err.message); }
    loadPlatformStats();
  }

  async function loadPlatformStats() {
    const list = $('#platform-list');
    if (!list) return;
    try {
      const data = await api('/api/admin/platform-stats');
      const items = data.items || [];
      if (!items.length) {
        list.innerHTML = '<li><span class="hint">近 14 天还没有访问</span><b>0</b></li>';
        return;
      }
      list.innerHTML = items.map((i) => `
        <li><span><i class="plat-dot" data-plat="${escapeHtml(i.key)}"></i>${escapeHtml(i.label)}</span>
        <b>${i.count}${i.blocked ? ` <em class="hint">拦截 ${i.blocked}</em>` : ''}</b></li>`).join('');
    } catch (err) { toast(err.message); }
    loadBlockedEvents();
  }

  async function loadBlockedEvents() {
    const list = $('#blocked-list');
    if (!list) return;
    try {
      const data = await api('/api/admin/blocked-events?limit=8');
      const items = data.items || [];
      if (!items.length) {
        list.innerHTML = '<li><span class="hint">还没有被拦下的访问</span><b>0</b></li>';
        return;
      }
      list.innerHTML = items.map((i) => `
        <li title="${escapeHtml(i.ua)}"><span><i class="plat-dot" data-plat="${escapeHtml(i.platform || 'other')}"></i>
        ${escapeHtml(i.label)} · <span class="mono">/${escapeHtml(i.code)}</span></span>
        <b>${escapeHtml(shortTime(i.ts))}</b></li>`).join('');
    } catch (err) { toast(err.message); }
  }

  function shortTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function bindSocial() {
    const form = $('#social-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const rules = {};
      form.querySelectorAll('select[data-plat]').forEach((el) => { rules[el.dataset.plat] = el.value; });
      setStatus('#social-status', '保存中…');
      try {
        await post('/api/admin/settings', { social_guard: $('#social-enabled').checked });
        const r = await post('/api/admin/social-rules', { rules, hint: $('#social-hint').value });
        $('#social-hint').value = r.hint || '';
        setStatus('#social-status', '已保存', 'ok');
        toast('社交平台防洪已更新');
        loadPlatformStats();
      } catch (err) { setStatus('#social-status', err.message, 'err'); }
    });
  }

  function bindGuard() {
    const gf = $('#guard-form');
    if (gf) gf.addEventListener('submit', async (e) => {
      e.preventDefault();
      setStatus('#guard-status', '保存中…');
      try {
        await post('/api/admin/settings', {
          redirect_rate_limit: parseInt($('#guard-redirect').value, 10) || 0,
          click_daily_limit: parseInt($('#guard-click').value, 10) || 0,
        });
        setStatus('#guard-status', '已保存', 'ok');
        toast('防洪策略已更新');
      } catch (err) { setStatus('#guard-status', err.message, 'err'); }
    });
    const uf = $('#ua-form');
    if (uf) uf.addEventListener('submit', async (e) => {
      e.preventDefault();
      setStatus('#ua-status', '保存中…');
      try {
        const r = await post('/api/admin/ua-rules', { rules: $('#ua-rules').value });
        $('#ua-rules').value = (r.rules || []).join('\n');
        setStatus('#ua-status', `已保存 ${r.rules.length} 条规则`, 'ok');
        toast('UA 屏蔽规则已更新');
      } catch (err) { setStatus('#ua-status', err.message, 'err'); }
    });
  }

  /* ---------- API 密钥 ---------- */
  async function loadKeys() {
    try {
      const keys = await api('/api/admin/api-keys');
      $('#keys-body').innerHTML = keys.map((k) => `<tr>
        <td>${escapeHtml(k.name)}</td>
        <td><code class="key-cell">${escapeHtml(k.key)}</code></td>
        <td>${k.enabled ? '<span class="badge on">启用</span>' : '<span class="badge off">停用</span>'}</td>
        <td class="created-cell">${k.last_used_at ? new Date(k.last_used_at).toLocaleString() : '未使用'}</td>
        <td><button class="btn tiny danger" data-key="${k.id}">删除</button></td>
      </tr>`).join('');
      $('#keys-empty').hidden = keys.length > 0;
    } catch (err) { toast(err.message); }
  }

  function bindKeys() {
    const nb = $('#new-key-btn');
    if (nb) nb.addEventListener('click', async () => {
      try {
        const k = await post('/api/admin/api-keys', {});
        toast('已创建密钥，请及时复制');
        await navigator.clipboard?.writeText(k.key).catch(() => {});
        loadKeys();
      } catch (err) { toast(err.message); }
    });
    const body = $('#keys-body');
    if (body) body.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-key]');
      if (!btn) return;
      if (!confirm('确定删除该密钥？')) return;
      try {
        await api('/api/admin/api-keys/' + btn.dataset.key, { method: 'DELETE' });
        toast('已删除');
        loadKeys();
      } catch (err) { toast(err.message); }
    });
  }

  /* ---------- 迁移 ---------- */
  function bindMigrate() {
    const form = $('#migrate-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = $('#migrate-payload').value;
      if (!payload.trim()) { setStatus('#migrate-status', '先粘贴点数据', 'err'); return; }
      const btn = $('#migrate-btn');
      btn.disabled = true;
      setStatus('#migrate-status', '解析并导入中…');
      try {
        const r = await post('/api/admin/migrate/yourls', { payload });
        setStatus('#migrate-status', `导入 ${r.imported} 条，换码 ${r.renamed}，失败 ${r.failed}`, 'ok');
        toast(`迁移完成：${r.imported} 条`);
      } catch (err) {
        setStatus('#migrate-status', err.message, 'err');
      } finally { btn.disabled = false; }
    });
  }

  /* ---------- 设置 ---------- */
  async function loadSettings() {
    try {
      const s = await api('/api/admin/settings');
      $('#set-site').value = s.site_name;
      $('#set-create').value = s.create_daily_limit;
      $('#set-code-length').value = s.code_length;
      $('#set-fetch').checked = !!s.fetch_title;
      $('#set-require-key').checked = !!s.require_api_key;
      $('#set-access-mode').value = s.access_mode || 'open';
      $('#access-mode-hint').textContent = (s.access_mode || 'open') === 'admin'
        ? '当前：访客仅能跳转短链接，创建、链接库与统计页均需登录后台。'
        : '当前：访客可创建短链接；链接库与统计仅管理员可见，创建者凭回执链接查看自己那条。';
      $('#flush-interval').textContent = s.click_flush_interval;
      $('#pending-count').textContent = s.pending_clicks;
    } catch (err) { toast(err.message); }
  }

  function bindSettings() {
    const modeSel = $('#set-access-mode');
    if (modeSel) {
      modeSel.addEventListener('change', () => {
        $('#access-mode-hint').textContent = modeSel.value === 'admin'
          ? '保存后：访客仅能跳转短链接，创建、链接库与统计页均需登录后台。'
          : '保存后：访客可创建短链接；链接库与统计仅管理员可见，创建者凭回执链接查看自己那条。';
      });
    }
    const form = $('#settings-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      setStatus('#settings-status', '保存中…');
      try {
        await post('/api/admin/settings', {
          site_name: ($('#set-site').value || '').trim() || 'SimpleURL',
          create_daily_limit: parseInt($('#set-create').value, 10) || 0,
          code_length: Math.min(16, Math.max(3, parseInt($('#set-code-length').value, 10) || 4)),
          fetch_title: $('#set-fetch').checked,
          require_api_key: $('#set-require-key').checked,
          access_mode: $('#set-access-mode').value,
        });
        setStatus('#settings-status', '已保存', 'ok');
        toast('设置已保存');
        loadSettings();
      } catch (err) { setStatus('#settings-status', err.message, 'err'); }
    });
    const rs = $('#reset-settings');
    if (rs) rs.addEventListener('click', async () => {
      if (!confirm('恢复所有设置为环境变量默认值？')) return;
      try { await post('/api/admin/settings/reset', {}); toast('已恢复默认'); loadSettings(); }
      catch (err) { toast(err.message); }
    });
    const fb = $('#flush-btn');
    if (fb) fb.addEventListener('click', async () => {
      try {
        const r = await post('/api/admin/flush-clicks', {});
        toast(`已结算 ${r.written} 条点击`);
        loadDashboard();
      } catch (err) { toast(err.message); }
    });
  }

  function bindAudit() {
    const sel = $('#audit-action');
    if (sel) sel.addEventListener('change', loadAudit);
  }

  /* ---------- 启动 ---------- */
  async function boot() {
    bindTabs(); bindAdminLinks(); bindGuard(); bindAudit(); bindSocial(); bindKeys(); bindMigrate(); bindSettings();
    await loadDashboard();
  }

  document.addEventListener('DOMContentLoaded', async () => {
    bindLogin();
    try {
      const s = await api('/api/admin/session');
      if (s.admin) { hideLogin(); boot(); }
      else if (s.enabled) { showLogin(); }
      else {
        // 没配 ADMIN_TOKEN：后台不可用，给个提示
        const g = $('#admin-login');
        if (g) {
          g.hidden = false;
          $('#admin-login-form').innerHTML =
            '<img src="/static/logo/icon.svg" alt="" width="40" height="40" /><h3>后台未启用</h3><p class="hint">请设置 ADMIN_TOKEN 环境变量后重启</p>';
        }
      }
    } catch (_) { showLogin(); }
  });
})();

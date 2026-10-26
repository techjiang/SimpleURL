/* SimpleURL 管理后台逻辑 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const boot = JSON.parse(($('#admin-bootstrap') || {}).textContent || '{}');
  const panel = boot.panel || 'dashboard';

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

  function setStatus(sel, msg, kind) {
    const el = $(sel);
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ credentials: 'same-origin' }, options));
    let data = {};
    try { data = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok) {
      const err = new Error(data.message || `请求失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  const fmtTime = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  const KIND_LABEL = { rate: '限速', ua: 'UA 屏蔽', wechat: '微信', qq: 'QQ' };

  /* ---------- 图表 ---------- */
  function renderChart(el, daily) {
    if (!el) return;
    const max = Math.max(1, ...daily.map((d) => d.count));
    el.innerHTML = daily
      .map((d) => `<div class="bar" title="${d.date}：${d.count} 次"><i style="height:${Math.max(2, (d.count / max) * 100)}%"></i><b>${d.date.slice(5)}</b></div>`)
      .join('');
  }

  /* ---------- 仪表盘 ---------- */
  async function loadDashboard() {
    let data;
    try {
      data = await api('/api/admin/overview');
    } catch (err) {
      toast(err.message);
      return;
    }
    const cards = [
      ['总链接', data.total_links, `${data.enabled_links} 条启用中`],
      ['总点击', data.total_clicks, `24 小时 ${data.clicks_today} 次`],
      ['近 14 天点击', data.clicks_recent, `${data.visitors_recent} 位独立访客`],
      ['待落库点击', data.queue.queued, `已写入 ${data.queue.flushed} 次`],
    ];
    const box = $('#dash-cards');
    if (box) {
      box.innerHTML = cards.map(([label, value, sub]) =>
        `<div class="stat-card"><span class="stat-label">${escapeHtml(label)}</span><strong>${value}</strong><span class="stat-sub">${escapeHtml(sub)}</span></div>`
      ).join('');
    }
    renderChart($('#dash-chart'), data.daily);

    const top = $('#dash-top');
    if (top) {
      top.innerHTML = data.top_links.length
        ? data.top_links.map((l) => `<li><span>/${escapeHtml(l.code)} <em class="hint">${escapeHtml((l.title || l.target || '').slice(0, 28))}</em></span><b>${l.clicks}</b></li>`).join('')
        : '<li class="empty-row">还没有点击数据</li>';
    }
    const g = $('#dash-guard');
    if (g) {
      g.innerHTML = ['rate', 'ua', 'wechat', 'qq']
        .map((kind) => `<li><span>${KIND_LABEL[kind] || kind}</span><b>${data.guard[kind] || 0}</b></li>`)
        .join('');
    }
    const badge = $('#guard-badge');
    if (badge) badge.textContent = `单IP ${data.guard.per_minute}/分 · 封 ${data.guard.block}s`;
    const buf = $('#buffer-line');
    if (buf) buf.textContent = `缓冲队列 ${data.queue.queued} 条 · 已结算 ${data.queue.batches} 批`;

    const body = $('#event-body');
    if (body) {
      body.innerHTML = data.events.length
        ? data.events.map((e) => `<tr><td class="hint">${fmtTime(e.ts)}</td><td class="code-cell">/${escapeHtml(e.code || '—')}</td><td>${escapeHtml(KIND_LABEL[e.kind] || e.kind)}</td><td class="hint">${escapeHtml(e.detail || '')}</td><td class="ua-cell">${escapeHtml(e.ua || '')}</td></tr>`).join('')
        : '<tr><td colspan="5" class="hint" style="text-align:center">暂无风控事件</td></tr>';
    }
  }

  /* ---------- 链接管理 ---------- */
  function bindAdminLinks() {
    const body = $('#admin-links-body');
    if (body) {
      body.addEventListener('click', async (e) => {
        const toggle = e.target.closest('[data-toggle]');
        const del = e.target.closest('[data-del]');
        try {
          if (toggle) {
            await api('/api/admin/links/' + encodeURIComponent(toggle.dataset.toggle) + '/toggle', { method: 'POST' });
            toast('状态已切换');
            location.reload();
          } else if (del) {
            const code = del.dataset.del;
            if (!confirm(`确定删除 /${code}？不可恢复`)) return;
            await api('/api/admin/links/' + encodeURIComponent(code), { method: 'DELETE' });
            toast('已删除');
            del.closest('tr').remove();
          }
        } catch (err) { toast(err.message); }
      });
    }
    const exportBtn = $('#export-json');
    if (exportBtn) {
      exportBtn.addEventListener('click', async () => {
        try {
          const data = await api('/api/admin/export');
          const blob = new Blob([JSON.stringify(data.items, null, 2)], { type: 'application/json' });
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = 'simpleurl-links.json';
          a.click();
          URL.revokeObjectURL(a.href);
          toast('已导出');
        } catch (err) { toast(err.message); }
      });
    }
  }

  /* ---------- 配置表单 ---------- */
  const SELECT_OPTIONS = {
    access_mode: [['public', '开放外部可使用'], ['admin', '仅管理员可使用'], ['invite', '邀请码可使用']],
    wechat_mode: [['warn', '提示 + 放行'], ['blocked', '一律拦截'], ['off', '不处理']],
    qq_mode: [['warn', '提示 + 放行'], ['blocked', '一律拦截'], ['off', '不处理']],
  };

  function fieldOf(item) {
    const id = 'set-' + item.key;
    const value = item.value;
    if (item.kind === 'bool') {
      return `<label class="check"><input type="checkbox" id="${id}" data-key="${item.key}" ${value ? 'checked' : ''} /> ${escapeHtml(item.label)}</label>
        ${item.help ? `<span class="hint">${escapeHtml(item.help)}</span>` : ''}`;
    }
    if (item.kind === 'select') {
      const opts = (SELECT_OPTIONS[item.key] || []).map(([v, t]) =>
        `<option value="${v}" ${String(value) === v ? 'selected' : ''}>${t}</option>`).join('');
      return `<label class="field-label" for="${id}">${escapeHtml(item.label)}</label>
        <select id="${id}" data-key="${item.key}">${opts}</select>
        ${item.help ? `<span class="hint">${escapeHtml(item.help)}</span>` : ''}`;
    }
    if (item.kind === 'int') {
      return `<label class="field-label" for="${id}">${escapeHtml(item.label)}</label>
        <input id="${id}" data-key="${item.key}" data-kind="int" type="number" value="${value}" min="${item.lo ?? 0}" max="${item.hi ?? ''}" />
        ${item.help ? `<span class="hint">${escapeHtml(item.help)}</span>` : ''}`;
    }
    return `<label class="field-label" for="${id}">${escapeHtml(item.label)}</label>
      <input id="${id}" data-key="${item.key}" type="text" value="${escapeHtml(value)}" />
      ${item.help ? `<span class="hint">${escapeHtml(item.help)}</span>` : ''}`;
  }

  function renderSettings(items) {
    const box = $('#settings-form');
    if (!box) return;
    box.innerHTML = items.map((item) => `<div class="panel-cell">${fieldOf(item)}</div>`).join('');
  }

  async function loadSettings() {
    try {
      const data = await api('/api/admin/settings');
      renderSettings(data.items);
      return data.values;
    } catch (err) { toast(err.message); }
    return {};
  }

  function collectSettings() {
    const out = {};
    $$('#settings-form [data-key]').forEach((el) => {
      if (el.type === 'checkbox') out[el.dataset.key] = el.checked;
      else if (el.dataset.kind === 'int') out[el.dataset.key] = Number(el.value);
      else out[el.dataset.key] = el.value;
    });
    return out;
  }

  function bindSettings() {
    const btn = $('#settings-save');
    if (btn) {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        setStatus('#settings-status', '保存中…');
        try {
          await api('/api/admin/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(collectSettings()),
          });
          setStatus('#settings-status', '已保存，立即生效', 'ok');
          toast('设置已保存');
        } catch (err) {
          setStatus('#settings-status', err.message, 'err');
        } finally { btn.disabled = false; }
      });
    }
  }

  /* ---------- 防洪面板 ---------- */
  const GUARD_KEYS = ['guard_ip_per_minute', 'guard_ip_daily', 'guard_code_per_minute', 'guard_block_seconds', 'wechat_mode', 'qq_mode'];

  async function loadGuard() {
    const data = await api('/api/admin/settings').catch(() => ({ items: [] }));
    const items = (data.items || []).filter((i) => GUARD_KEYS.includes(i.key));
    const box = $('#guard-form');
    if (box) box.innerHTML = items.map((item) => `<div class="panel-cell">${fieldOf(item)}</div>`).join('');

    const ua = (data.items || []).find((i) => i.key === 'blocked_uas');
    const editor = $('#ua-editor');
    if (editor && ua) editor.value = ua.value || '';

    const overview = await api('/api/admin/overview').catch(() => null);
    if (overview) {
      const state = $('#guard-state');
      if (state) {
        state.innerHTML = [
          ['单 IP 每分钟', overview.guard.per_minute + ' 次'],
          ['单 IP 每日', overview.guard.daily + ' 次'],
          ['单短码每分钟', overview.guard.per_code ? overview.guard.per_code + ' 次' : '不限制'],
          ['封禁时长', overview.guard.block + ' 秒'],
          ['微信内打开', overview.guard.wechat_mode],
          ['QQ 内打开', overview.guard.qq_mode],
          ['UA 规则', overview.guard.ua_rules + ' 条'],
          ['待落库', overview.queue.queued + ' 条'],
        ].map(([k, v]) => `<li><span>${k}</span><b>${escapeHtml(String(v))}</b></li>`).join('');
      }
      const body = $('#event-body-2');
      if (body) {
        body.innerHTML = overview.events.length
          ? overview.events.map((e) => `<tr><td class="hint">${fmtTime(e.ts)}</td><td class="code-cell">/${escapeHtml(e.code || '—')}</td><td>${escapeHtml(KIND_LABEL[e.kind] || e.kind)}</td><td class="hint">${escapeHtml(e.detail || '')}</td></tr>`).join('')
          : '<tr><td colspan="4" class="hint" style="text-align:center">暂无事件</td></tr>';
      }
    }
  }

  function bindGuard() {
    const save = $('#ua-save');
    if (save) {
      save.addEventListener('click', async () => {
        try {
          await api('/api/admin/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ blocked_uas: $('#ua-editor').value }),
          });
          setStatus('#ua-status', '已保存', 'ok');
        } catch (err) { setStatus('#ua-status', err.message, 'err'); }
      });
    }
    const form = $('#guard-form');
    if (form) {
      form.addEventListener('change', async () => {
        const out = {};
        $$('#guard-form [data-key]').forEach((el) => {
          out[el.dataset.key] = el.type === 'checkbox' ? el.checked
            : el.dataset.kind === 'int' ? Number(el.value) : el.value;
        });
        try {
          await api('/api/admin/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(out),
          });
          toast('防洪设置已生效');
        } catch (err) { toast(err.message); }
      });
    }
    ['#flush-now', '#flush-now-2'].forEach((sel) => {
      const btn = $(sel);
      if (btn) btn.addEventListener('click', async () => {
        try {
          const data = await api('/api/admin/flush', { method: 'POST' });
          toast(`已结算 ${data.written} 条`);
          setTimeout(() => location.reload(), 400);
        } catch (err) { toast(err.message); }
      });
    });
  }

  /* ---------- API 密钥 ---------- */
  async function loadKeys() {
    const body = $('#key-body');
    if (!body) return;
    try {
      const data = await api('/api/admin/keys');
      body.innerHTML = data.items.length
        ? data.items.map((k) => `<tr>
            <td>${escapeHtml(k.name)}</td>
            <td><code>${escapeHtml(k.prefix)}…</code></td>
            <td>${fmtTime(k.created_at)}</td>
            <td>${fmtTime(k.last_used_at)}</td>
            <td><button class="btn tiny danger" data-key-del="${k.id}">删除</button></td>
          </tr>`).join('')
        : '<tr><td colspan="5" class="hint">还没有密钥</td></tr>';
    } catch (err) { toast(err.message); }
  }

  function bindKeys() {
    const create = $('#key-create');
    if (create) {
      create.addEventListener('click', async () => {
        const name = ($('#key-name').value || '').trim() || '未命名';
        try {
          const data = await api('/api/admin/keys', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
          });
          const box = $('#key-secret');
          box.hidden = false;
          box.innerHTML = `只显示这一次，请立即保存：<code>${escapeHtml(data.key)}</code>`;
          setStatus('#key-status', '已生成', 'ok');
          loadKeys();
        } catch (err) { setStatus('#key-status', err.message, 'err'); }
      });
    }
    const body = $('#key-body');
    if (body) {
      body.addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-key-del]');
        if (!btn) return;
        if (!confirm('删除后使用该密钥的调用方会立刻失效，确定？')) return;
        try {
          await api('/api/admin/keys/' + btn.dataset.keyDel, { method: 'DELETE' });
          loadKeys();
        } catch (err) { toast(err.message); }
      });
    }
  }

  /* ---------- 迁移 ---------- */
  function bindMigrate() {
    const run = $('#migrate-run');
    if (!run) return;
    run.addEventListener('click', async () => {
      const content = $('#migrate-input').value;
      if (!content.trim()) { setStatus('#migrate-status', '先粘贴内容', 'err'); return; }
      run.disabled = true;
      setStatus('#migrate-status', '导入中，数据多时需要一会儿…');
      try {
        const data = await api('/api/admin/migrate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content, conflict: $('#migrate-skip').checked ? 'skip' : 'rename' }),
        });
        setStatus('#migrate-status', `解析 ${data.parsed} · 新建 ${data.created} · 跳过 ${data.skipped} · 失败 ${data.failed}`, 'ok');
        const box = $('#migrate-result');
        if (box && data.samples && data.samples.length) {
          box.innerHTML = data.samples.map((s) =>
            `<li><span>${escapeHtml(s.from ? `/${s.from} → /${s.to}` : (s.error || ''))}</span></li>`).join('');
        }
      } catch (err) {
        setStatus('#migrate-status', err.message, 'err');
      } finally { run.disabled = false; }
    });
  }

  /* ---------- 危险操作 ---------- */
  function bindDanger() {
    const status = (msg, kind) => {
      const el = $('#danger-status');
      if (el) { el.textContent = msg; el.className = 'buffer-line' + (kind ? ' ' + kind : ''); }
    };
    const reset = $('#reset-settings');
    if (reset) reset.addEventListener('click', async () => {
      if (!confirm('重置后所有后台改过的配置会回到环境变量/默认值，确定？')) return;
      try {
        await api('/api/admin/settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ site_name: 'SimpleURL', announcement: '', blocked_uas: '', access_mode: 'public' }),
        });
        status('已重置关键项，刷新后生效', 'ok');
      } catch (err) { status(err.message, 'err'); }
    });

    const clearClicks = $('#clear-clicks');
    if (clearClicks) clearClicks.addEventListener('click', async () => {
      if (!confirm('清零统计：所有链接的点击数和访问明细都会归零，确定？')) return;
      try {
        await api('/api/admin/danger/clear-clicks', { method: 'POST' });
        status('统计已清零', 'ok');
      } catch (err) { status(err.message, 'err'); }
    });

    const clearAll = $('#clear-all');
    if (clearAll) clearAll.addEventListener('click', async () => {
      if (!confirm('删除全部链接与统计，此操作不可恢复，确定？')) return;
      if (!confirm('再确认一次：真的要清空整个链接库？')) return;
      try {
        await api('/api/admin/danger/clear-all', { method: 'POST' });
        status('链接库已清空', 'ok');
      } catch (err) { status(err.message, 'err'); }
    });
  }

  /* ---------- 启动 ---------- */
  document.addEventListener('DOMContentLoaded', () => {
    const logout = $('#admin-logout');
    if (logout) logout.addEventListener('click', async () => {
      await api('/api/admin/logout', { method: 'POST' }).catch(() => {});
      location.href = '/';
    });

    if (panel === 'dashboard') loadDashboard();
    if (panel === 'links') bindAdminLinks();
    if (panel === 'guard') { bindGuard(); loadGuard().catch((err) => toast(err.message)); }
    if (panel === 'keys') { bindKeys(); loadKeys(); }
    if (panel === 'migrate') bindMigrate();
    if (panel === 'settings') { bindDanger(); bindSettings(); if (boot.settings && boot.settings.length) renderSettings(boot.settings); else loadSettings(); }
  });
})();

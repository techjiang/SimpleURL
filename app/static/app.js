/* SimpleURL 前端逻辑 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const state = { page: 1, pageSize: 10, q: '', status: '', sort: 'created_desc', total: 0 };
  let summary = { total_links: 0, total_clicks: 0 };

  /* ---------- 工具 ---------- */
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
    toastTimer = setTimeout(() => { el.hidden = true; }, 2400);
  }

  function setStatus(msg, kind) {
    const el = $('#create-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  const fmtDate = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };

  async function api(path, options) {
    const res = await fetch(path, options);
    let data = {};
    try { data = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) throw new Error(data.message || `请求失败（${res.status}）`);
    return data;
  }

  /* ---------- 创建 ---------- */
  function bindCreate() {
    const form = $('#create-form');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('#submit-btn');
      const payload = {
        target: $('#target').value.trim(),
        code: $('#code').value.trim() || undefined,
        title: $('#title').value.trim() || undefined,
        tags: $('#tags').value.trim() || undefined,
        expires_at: $('#expires_at').value || undefined,
      };
      if (!payload.target) { setStatus('请先粘贴一个链接', 'err'); return; }
      btn.disabled = true;
      setStatus('正在生成…');
      try {
        const link = await api('/api/links', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        showResult(link);
        setStatus('创建成功', 'ok');
        form.reset();
        loadSummary();
        loadLinks();
      } catch (err) {
        setStatus(err.message, 'err');
        toast(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  function showResult(link) {
    const box = $('#result');
    if (!box) return;
    const a = $('#result-link');
    a.href = link.short_url;
    a.textContent = link.short_url;
    $('#result-qr').src = '/api/qr/' + encodeURIComponent(link.code);
    $('#result-tags').innerHTML = (link.tags || [])
      .map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    box.hidden = false;
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  async function loadSummary() {
    try {
      summary = await api('/api/stats/summary');
    } catch (_) {
      summary = { total_links: 0, total_clicks: 0 };
    }
  }

  function bindCopy() {
    const btn = $('#copy-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const text = ($('#result-link').textContent || '').trim();
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        const ta = document.createElement('textarea');
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      }
      btn.textContent = '已复制 ✓';
      toast('已复制到剪贴板');
      setTimeout(() => { btn.textContent = '复制'; }, 1600);
    });
  }

  /* ---------- 列表 ---------- */
  function badgeOf(link) {
    if (link.expired) return '<span class="badge exp">已过期</span>';
    return link.enabled ? '<span class="badge on">已启用</span>' : '<span class="badge off">已停用</span>';
  }

  function rowOf(link) {
    const tags = (link.tags || []).map((t) => `<span class="tag-pill">${escapeHtml(t)}</span>`).join('');
    const title = link.title ? `<span class="title-cell">${escapeHtml(link.title)}</span>` : '';
    return `<tr>
      <td><a class="code-cell" href="/s/${encodeURIComponent(link.code)}" title="查看统计">/${escapeHtml(link.code)}</a>${title}</td>
      <td class="target-cell" title="${escapeHtml(link.target)}">${escapeHtml(link.target)}</td>
      <td class="clicks-cell">${link.clicks}</td>
      <td>${tags || '<span class="hint">—</span>'}</td>
      <td>${badgeOf(link)}</td>
      <td>
        <a class="btn tiny ghost" href="/${encodeURIComponent(link.code)}" target="_blank" rel="noopener">访问</a>
        <button class="btn tiny ghost" data-act="toggle" data-code="${escapeHtml(link.code)}">${link.enabled ? '停用' : '启用'}</button>
        <button class="btn tiny danger" data-act="del" data-code="${escapeHtml(link.code)}">删除</button>
      </td>
    </tr>`;
  }

  async function loadLinks() {
    const body = $('#links-body');
    if (!body) return;
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, sort: state.sort });
    if (state.q) params.set('q', state.q);
    if (state.status) params.set('status', state.status);
    try {
      const data = await api('/api/links?' + params.toString());
      state.total = data.total;
      body.innerHTML = data.items.map(rowOf).join('');
      $('#empty').hidden = data.items.length > 0;
      const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
      $('#summary').innerHTML = [
        `<span class="chip">共 ${data.total} 条</span>`,
        `<span class="chip">总点击 ${summary.total_clicks}</span>`,
        `<span class="chip">第 ${data.page}/${pages} 页</span>`,
      ].join('');
      $('#page-info').textContent = `第 ${data.page} 页 / 共 ${Math.max(1, Math.ceil(data.total / state.pageSize))} 页`;
      $('#prev-btn').disabled = data.page <= 1;
      $('#next-btn').disabled = data.page * state.pageSize >= data.total;
    } catch (err) {
      toast(err.message);
    }
  }

  function bindBoard() {
    const body = $('#links-body');
    if (!body) return;
    let t = null;
    $('#search-input').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { state.q = e.target.value.trim(); state.page = 1; loadLinks(); }, 260);
    });
    $('#status-filter').addEventListener('change', (e) => { state.status = e.target.value; state.page = 1; loadLinks(); });
    $('#sort-select').addEventListener('change', (e) => { state.sort = e.target.value; state.page = 1; loadLinks(); });
    $('#prev-btn').addEventListener('click', () => { if (state.page > 1) { state.page -= 1; loadLinks(); } });
    $('#next-btn').addEventListener('click', () => { state.page += 1; loadLinks(); });

    body.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const code = btn.dataset.code;
      try {
        if (btn.dataset.act === 'del') {
          if (!confirm(`确定删除 /${code} ？该操作不可恢复`)) return;
          await api('/api/links/' + encodeURIComponent(code), { method: 'DELETE' });
          toast(`已删除 /${code}`);
        } else {
          const current = await api('/api/links/' + encodeURIComponent(code));
          await api('/api/links/' + encodeURIComponent(code), {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !current.enabled }),
          });
          toast(`/${code} 已${current.enabled ? '停用' : '启用'}`);
        }
        loadSummary();
        loadLinks();
      } catch (err) {
        toast(err.message);
      }
    });
  }

  /* ---------- 图表 ---------- */
  function renderChart(el, daily) {
    const max = Math.max(1, ...daily.map((d) => d.count));
    el.innerHTML = daily
      .map((d) => `<div class="bar" title="${d.date}：${d.count} 次点击"><i style="height:${(d.count / max) * 100}%"></i><b>${d.date.slice(5)}</b></div>`)
      .join('');
  }

  /* ---------- 启动 ---------- */
  document.addEventListener('DOMContentLoaded', () => {
    bindCreate(); bindCopy(); bindBoard(); loadSummary(); loadLinks();
  });

  window.SimpleURL = { renderChart };
})();

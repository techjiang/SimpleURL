/* 接口与工具页：把文档里的每个请求做成能直接点的那种 */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const base = () => ($('#api-base')?.textContent || '').replace(/api$/, '');

  function setStatus(msg, kind) {
    const el = $('#try-status');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  function csrf() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  function curlOf(target, code) {
    const body = { target };
    if (code) body.code = code;
    return [
      `curl -X POST ${base()}api/links \\`,
      "  -H 'Content-Type: application/json' \\",
      "  -H 'X-Api-Key: sk-...' \\",
      `  -d '${JSON.stringify(body)}'`,
    ].join('\n');
  }

  function bindTry() {
    const form = $('#try-form');
    if (!form) return;
    const targetEl = $('#try-target');
    const codeEl = $('#try-code');

    const refreshCurl = () => {
      $('#try-curl').textContent = curlOf(targetEl.value.trim() || 'https://example.com/a/very/long/link', codeEl.value.trim());
    };
    targetEl.addEventListener('input', refreshCurl);
    codeEl.addEventListener('input', refreshCurl);

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const target = targetEl.value.trim();
      if (!target) { setStatus('先填一个链接', 'err'); return; }
      const headers = { 'Content-Type': 'application/json' };
      const csrfToken = csrf();
      if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
      const key = ($('#try-key').value || '').trim();
      if (key) headers['X-Api-Key'] = key;
      const btn = $('#try-btn');
      btn.disabled = true;
      setStatus('请求中…');
      try {
        const res = await fetch('/api/links', {
          method: 'POST',
          headers,
          body: JSON.stringify({ target, code: codeEl.value.trim() || undefined }),
        });
        const data = await res.json().catch(() => ({}));
        $('#try-json').textContent = JSON.stringify(data, null, 2);
        if (!res.ok) throw new Error(data.message || `请求失败（${res.status}）`);
        const link = $('#try-link');
        link.href = data.short_url;
        link.textContent = data.short_url;
        $('#try-result').hidden = false;
        setStatus(`HTTP ${res.status}`, 'ok');
      } catch (err) {
        setStatus(err.message, 'err');
      } finally {
        btn.disabled = false;
      }
    });

    const copyBtn = $('#try-copy');
    if (copyBtn) copyBtn.addEventListener('click', async () => {
      const text = ($('#try-link').textContent || '').trim();
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = '已复制 ✓';
        setTimeout(() => { copyBtn.textContent = '复制'; }, 1600);
      } catch (_) { setStatus('复制失败，请手动选中文案', 'err'); }
    });
  }

  document.addEventListener('DOMContentLoaded', bindTry);
})();

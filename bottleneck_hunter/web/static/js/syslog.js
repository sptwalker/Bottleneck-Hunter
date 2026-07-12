(function () {
  'use strict';

  var MAX_ENTRIES = 50;
  var es = null;

  function initSyslog() {
    var scroll = document.getElementById('syslog-scroll');
    var dot = document.getElementById('syslog-dot');
    if (!scroll) return;

    wireBar();

    if (es) { es.close(); es = null; }

    es = new EventSource('/api/system/logs');

    es.addEventListener('log', function (e) {
      try {
        var d = JSON.parse(e.data);
        appendEntry(scroll, d);
        mirrorToPanel(d);
      } catch (_) {}
    });

    es.addEventListener('open', function () {
      if (dot) dot.classList.add('connected');
    });

    es.addEventListener('error', function () {
      if (dot) dot.classList.remove('connected');
    });
  }

  // 底部日志栏 → 点击弹出可读浮窗（稳定入口：footer 每页都在）
  function wireBar() {
    var bar = document.getElementById('syslog-bar');
    if (!bar || bar._wired) return;
    bar._wired = true;
    bar.style.cursor = 'pointer';
    bar.title = '点击查看完整日志输出';
    bar.addEventListener('click', function () {
      var panel = document.getElementById('wiz-log-panel');
      if (!panel) return;
      var hidden = panel.style.display === 'none' || !panel.style.display;
      panel.style.display = hidden ? 'block' : 'none';
      if (hidden) {
        panel.classList.remove('collapsed');
        var body = document.getElementById('wiz-log-body');
        if (body) body.scrollTop = body.scrollHeight;
      }
    });
  }

  // 把真实服务器日志镜像进浮窗体（与 phase 消息共存；不走 logMsg 以免每行都自动弹窗）
  function mirrorToPanel(d) {
    var body = document.getElementById('wiz-log-body');
    if (!body) return;
    var level = d.level || 'info';
    var cls = level === 'error' ? 'log-error' : (level === 'warning' || level === 'warn') ? 'log-warn' : '';
    var line = document.createElement('div');
    line.className = 'wiz-log-line ' + cls;
    line.innerHTML = '<span class="log-ts">[' + esc(d.ts || '') + ']</span> ' + esc(d.msg || '');
    body.appendChild(line);
    while (body.children.length > 300) body.removeChild(body.firstChild);
    var panel = document.getElementById('wiz-log-panel');
    if (panel && panel.style.display !== 'none') body.scrollTop = body.scrollHeight;
  }

  function appendEntry(scroll, d) {
    var el = document.createElement('span');
    var level = d.level || 'info';
    el.className = 'syslog-entry level-' + level;
    el.innerHTML = '<span class="syslog-ts">' + esc(d.ts || '') + '</span> ' + esc(d.msg || '');

    scroll.appendChild(el);

    while (scroll.children.length > MAX_ENTRIES) {
      scroll.removeChild(scroll.firstChild);
    }

    scroll.scrollLeft = scroll.scrollWidth;
  }

  function esc(s) {
    var d = document.createElement('span');
    d.textContent = s;
    return d.innerHTML;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSyslog);
  } else {
    initSyslog();
  }
})();

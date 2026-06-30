(function () {
  'use strict';

  var MAX_ENTRIES = 50;
  var es = null;

  function initSyslog() {
    var scroll = document.getElementById('syslog-scroll');
    var dot = document.getElementById('syslog-dot');
    if (!scroll) return;

    if (es) { es.close(); es = null; }

    es = new EventSource('/api/system/logs');

    es.addEventListener('log', function (e) {
      try {
        var d = JSON.parse(e.data);
        appendEntry(scroll, d);
      } catch (_) {}
    });

    es.addEventListener('open', function () {
      if (dot) dot.classList.add('connected');
    });

    es.addEventListener('error', function () {
      if (dot) dot.classList.remove('connected');
    });
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

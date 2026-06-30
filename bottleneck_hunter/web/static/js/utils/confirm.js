/**
 * 统一确认对话框 — 替代原生 confirm()
 */

let _resolve = null;

function getEls() {
  return {
    overlay: document.getElementById('confirm-modal'),
    title:   document.getElementById('confirm-title'),
    msg:     document.getElementById('confirm-message'),
    ok:      document.getElementById('confirm-ok'),
    cancel:  document.getElementById('confirm-cancel'),
  };
}

function close(result) {
  const { overlay } = getEls();
  overlay.style.display = 'none';
  if (_resolve) { _resolve(result); _resolve = null; }
}

function init() {
  const { overlay, cancel, ok } = getEls();
  if (!overlay) return;

  cancel.addEventListener('click', () => close(false));
  ok.addEventListener('click', () => close(true));
  overlay.addEventListener('click', e => {
    if (e.target === overlay) close(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && overlay.style.display !== 'none') close(false);
  });
}

/**
 * @param {string} message
 * @param {object} [opts]
 * @param {string} [opts.title='确认操作']
 * @param {string} [opts.confirmText='确定']
 * @param {string} [opts.cancelText='取消']
 * @param {boolean} [opts.danger=false]
 * @returns {Promise<boolean>}
 */
export function showConfirm(message, opts = {}) {
  const { overlay, title, msg, ok, cancel } = getEls();
  title.textContent   = opts.title || '确认操作';
  msg.textContent     = message;
  ok.textContent      = opts.confirmText || '确定';
  cancel.textContent  = opts.cancelText || '取消';

  ok.classList.toggle('btn-danger', !!opts.danger);
  ok.classList.toggle('btn-primary', !opts.danger);

  overlay.style.display = '';
  ok.focus();

  return new Promise(resolve => { _resolve = resolve; });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

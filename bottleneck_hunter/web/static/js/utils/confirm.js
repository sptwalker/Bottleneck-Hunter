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
  const { overlay, ok } = getEls();
  overlay.style.display = 'none';
  ok.style.display = '';                                  // 复原：showChoice 会隐藏确定按钮
  const box = document.getElementById('confirm-choices'); // 复原：清掉选项列表
  if (box) box.innerHTML = '';
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
  // cancelText 显式传空字符串 → 隐藏取消按钮（单按钮提示框，如"分析被迫停止"）
  if (opts.cancelText === '') {
    cancel.style.display = 'none';
  } else {
    cancel.style.display = '';
    cancel.textContent = opts.cancelText || '取消';
  }

  ok.classList.toggle('btn-danger', !!opts.danger);
  ok.classList.toggle('btn-primary', !opts.danger);

  overlay.style.display = '';
  ok.focus();

  return new Promise(resolve => { _resolve = resolve; });
}

/**
 * 单选对话框 — 复用确认弹窗外壳，列出选项按钮，点击即选中。
 * @param {string} message 顶部提示文案
 * @param {Array<{value:*, label:string, sub?:string}>} choices 选项
 * @param {object} [opts]
 * @param {string} [opts.title='请选择']
 * @param {string} [opts.cancelText='取消']
 * @returns {Promise<*|null>} 选中项的 value；取消/关闭返回 null
 */
export function showChoice(message, choices, opts = {}) {
  const { overlay, title, msg, ok, cancel } = getEls();
  if (!overlay) return Promise.resolve(null);
  title.textContent = opts.title || '请选择';
  msg.textContent = message;
  ok.style.display = 'none';                     // 选择由点击选项完成，无通用确定
  cancel.style.display = '';
  cancel.textContent = opts.cancelText || '取消';

  let box = document.getElementById('confirm-choices');
  if (!box) {
    box = document.createElement('div');
    box.id = 'confirm-choices';
    box.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin-top:12px;';
    msg.insertAdjacentElement('afterend', box);
  }
  box.innerHTML = '';
  (choices || []).forEach(ch => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-sm';
    b.style.cssText = 'width:100%;text-align:left;';
    const strong = document.createElement('b');
    strong.textContent = ch.label;               // textContent 防注入（账户名来自用户数据）
    b.appendChild(strong);
    if (ch.sub) {
      const s = document.createElement('span');
      s.style.color = 'var(--muted)';
      s.textContent = ` · ${ch.sub}`;
      b.appendChild(s);
    }
    b.addEventListener('click', () => close(ch.value));
    box.appendChild(b);
  });

  overlay.style.display = '';
  cancel.focus();
  return new Promise(resolve => { _resolve = resolve; });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

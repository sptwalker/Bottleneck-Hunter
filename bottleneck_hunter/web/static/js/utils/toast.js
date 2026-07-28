/* 统一 Toast 通知 —— 全站唯一实现。
 * 替代历史三套：dashboard._showToast / watchlist.showToast / wizard-state.toast。
 * 复用 .bh-toast 样式（css/watchlist/watchlist.css）。单例元素，后到覆盖前者。
 *
 * 用法：toast('保存成功')            // 不传 type → 按文案自动判级
 *      toast('操作失败', 'error')   // 显式指定
 * type ∈ success | warning | error | info。替代 alert() 时可直接 toast(msg)——
 * 文案里的「失败/请/成功」会被识别成对应色，动态消息（如 alert(msg)）也能正确判级。
 */

const _DUR = { error: 5000, warning: 4000, success: 3000, info: 3000 };

function _autoType(s) {
  if (/失败|错误|异常|无法|拦截|超时|不可|禁止|不能|未返回|被拒/.test(s)) return 'error';
  if (/成功|已保存|已更新|已完成|已清空|已删除|已提交|已自动保存/.test(s)) return 'success';
  if (/^请|请输入|请选择|请先|请至少|请填写|请允许/.test(s)) return 'warning';
  return 'info';
}

export function toast(msg, type, duration) {
  const text = String(msg == null ? '' : msg);
  let t = type || _autoType(text);
  if (t === 'warn') t = 'warning';  // 兼容历史 notifyFallback 的 'warn'
  let el = document.getElementById('bh-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'bh-toast';
    document.body.appendChild(el);
  }
  el.className = `bh-toast bh-toast--${t} bh-toast--show`;
  el.textContent = text;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('bh-toast--show'), duration || _DUR[t] || 3000);
}

// 自检（仅 node 直接运行时；浏览器 import 因 document 存在而跳过）：
//   node bottleneck_hunter/web/static/js/utils/toast.js
if (typeof document === 'undefined' && typeof process !== 'undefined'
    && process.argv[1] && process.argv[1].replace(/\\/g, '/').endsWith('utils/toast.js')) {
  console.assert(_autoType('操作失败: x') === 'error', 'error');
  console.assert(_autoType('连接成功！') === 'success', 'success');
  console.assert(_autoType('请输入名称') === 'warning', 'warning');
  console.assert(_autoType('已清空 3 条操作') === 'success', 'cleared→success');
  console.assert(_autoType('未达限价，已转为挂单等待成交') === 'info', 'resting→info');
  console.log('toast _autoType self-check OK');
}

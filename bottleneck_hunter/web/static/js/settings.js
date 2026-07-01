/**
 * settings.js — Provider 状态共享接口。
 *
 * 旧的「API 设置」弹窗已移除：API Key 与自定义端点现统一在顶部「AI 配置中心」配置。
 * 此模块仅保留向导模型下拉填充所需的 Provider 状态读取/订阅接口（供 phases.js 使用）。
 */

let providers = [];
let _onProvidersChange = null;

export function getProviders() {
  return providers;
}

export function onProvidersChange(fn) {
  _onProvidersChange = fn;
}

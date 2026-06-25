/**
 * sse.js — 通用 SSE 流读取器（含断连重试）
 */

export async function readSSEStream(url, body, { onEvent, onTick, onError, label = 'sse', maxRetries = 3, logFn = null, getAnalysisId = null } = {}) {
  const delays = [1000, 3000, 5000];
  let attempt = 0;
  let receivedAny = false;

  while (attempt <= maxRetries) {
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let sseEvent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        receivedAny = true;
        attempt = 0;
        if (onTick) onTick();
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('event:')) {
            sseEvent = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            try {
              const data = JSON.parse(line.slice(5).trim());
              data._sseEvent = sseEvent;
              if (onEvent) onEvent(data);
            } catch (e) { console.warn(`[${label}] JSON解析失败:`, e.message, line.slice(0, 200)); }
            sseEvent = '';
          } else if (line.trim() === '') {
            sseEvent = '';
          }
        }
      }
      if (buffer.trim().startsWith('data:')) {
        try {
          const data = JSON.parse(buffer.trim().slice(5).trim());
          if (onEvent) onEvent(data);
        } catch (e) { console.warn(`[${label}] 尾部JSON解析失败:`, e.message); }
      }
      return;
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      attempt++;
      if (attempt > maxRetries) {
        if (onError) onError(err);
        return;
      }
      const delay = delays[attempt - 1] || 5000;
      if (logFn) logFn(`[${label}] 连接中断，${delay / 1000}s 后重试 (${attempt}/${maxRetries})...`, 'warn');
      await new Promise(r => setTimeout(r, delay));

      const analysisId = getAnalysisId ? getAnalysisId() : null;
      if (analysisId) {
        try {
          const statusResp = await fetch(`/api/history/${analysisId}/phase-status`);
          if (statusResp.ok) {
            const statusData = await statusResp.json();
            if (statusData && statusData.completed) {
              if (logFn) logFn(`[${label}] 后端已完成，使用缓存数据`, 'info');
              return;
            }
          }
        } catch (_) {}
      }
    }
  }
}

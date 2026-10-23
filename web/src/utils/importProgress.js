export function importProgressSummary(task) {
  if (task?.status !== 'running' || !Number.isFinite(task.entries_per_second) || task.entries_per_second <= 0) return '';
  const rate = task.entries_per_second;
  const speed = rate < 1 ? rate.toFixed(2) : rate.toFixed(1);
  const remaining = Number.isInteger(task.remaining) && task.remaining >= 0 ? ` · 剩余 ${task.remaining} 条` : '';
  const seconds = task.eta_seconds;
  if (!Number.isFinite(seconds) || seconds < 0) return `近期约 ${speed} 条/秒${remaining} · 正在估算剩余时间`;
  const minutes = Math.ceil(seconds / 60);
  const duration = seconds < 60 ? '不足 1 分钟' : minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时${minutes % 60 ? ` ${minutes % 60} 分钟` : ''}`;
  return `近期约 ${speed} 条/秒${remaining} · 预计还需约 ${duration}（按近期速度估算）`;
}

const noMatchTriggers = new Set([
  "no_match",
  "gateway_no_match",
  "below_threshold",
  "insufficient_margin",
]);

export function resolveBridgeObservationOutcome({ injected = [], hookOutcome = "", trigger = "", routeAction = "" } = {}) {
  if (injected.length || hookOutcome === "injected") return "injected";
  if (hookOutcome === 'no_match' || noMatchTriggers.has(trigger)) return "no_match";
  return routeAction === "skip" ? "skip" : "skip";
}

export function resolveGatewayObservationOutcome(payload, legacyOutcome) {
  if (!payload?.observation_version) return legacyOutcome;
  if (['failed', 'interrupted'].includes(payload.request_status)) return 'failed';
  if (payload.request_status !== 'completed') return 'pending';
  if (payload.injected_bucket_ids?.length) return 'injected';
  return payload.recall_state === 'no_match' ? 'no_match' : 'skip';
}

export function gatewayRequestLabel(payload) {
  if (!payload?.observation_version) return '历史记录';
  const states = {preparing:'正在准备召回', upstream_pending:'等待完整回复；已准备的记忆尚未确认成功交付',
    failed:'请求失败，未确认成功交付', interrupted:'回复中断或缺少结束标志，未确认成功交付'};
  if (payload.request_status === 'completed') {
    states.completed = payload.injected_bucket_ids?.length
      ? `已向聊天模型注入 ${payload.injected_bucket_ids.length} 条记忆，上游回复已完成`
      : payload.recall_state === 'disabled' ? '自动召回已关闭，本轮未注入记忆'
      : payload.recall_state === 'no_match' ? '本轮没有选出可注入的记忆'
      : payload.recall_state === 'resume' ? '本轮使用续接上下文，没有执行自动召回'
      : '本轮未注入自动召回的记忆';
  }
  const reason = payload.recall_diagnostics?.reason;
  const reasons = {hook_deadline_before_reranker:'召回时间预算不足，未进入重排评分',hook_deadline_before_candidates:'召回时间预算不足，未检索候选',daily_surface_without_memory_intent:'本次没有明确的记忆需求'};
  const rerankerError = payload.recall_diagnostics?.reranker_error;
  const errors = {http_401:'重排服务认证失败（401），请检查模型密钥', http_403:'重排服务拒绝访问（403）', http_429:'重排服务限流（429）', timeout:'重排服务请求超时', request_failed:'重排服务连接失败'};
  const diagnostic = rerankerError ? errors[rerankerError] || '重排服务未返回有效分数' : reason ? reasons[reason] || reason : '';
  return [states[payload.request_status], diagnostic].filter(Boolean).join('；');
}

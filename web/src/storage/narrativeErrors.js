const reasons = {
  storage_busy: '记忆库正被其他写入占用，请稍后重试。',
  storage_unavailable: '记忆存储暂不可用，请稍后重试。',
  narrative_revision_changed: '这本书已有新修改，请重新打开后再操作。',
  revision_mismatch: '这本书已有新修改，请重新打开后再操作。',
  narrative_document_changed: '正文已在其他地方修改，请重新打开后再操作。',
  document_hash_mismatch: '正文已在其他地方修改，请重新打开后再操作。',
  material_snapshot_changed: '材料内容已变化，请重新预览后保存。',
  preview_fingerprint_mismatch: '正文或材料与预览不一致，请重新预览后保存。',
  narrative_not_found: '这本叙事卷已删除或不存在，请刷新书架。',
  requires_collecting_line: '这本书已写有正文，请在编辑区预览后保存。',
  bound_material_unavailable: '部分材料无法读取，请处理下列材料后重试。',
  at_least_two_narrative_materials_required: '至少需要保留两条材料。',
  at_least_two_source_scenes_required: '至少需要保留两条材料。',
  at_least_two_sources_required: '至少需要保留两条材料。',
  invalid_delete_request: '删除请求缺少确认或版本信息，请重新打开这本书。',
  invalid_body_save_request: '缺少有效正文或预览信息，请重新预览后保存。',
  invalid_expected_revision: '缺少有效版本信息，请重新打开这本书。',
  no_new_materials: '没有新增材料可用于更新。',
  update_requires_rewrite_after_material_removal: '删减材料后需要使用重写。',
  preview_job_missing: '预览任务已过期或服务已重启，请重新书写。',
  preview_busy: '已有三个书写任务正在运行，请稍后重试。',
};
const sourceReasons = {
  not_found: '已删除或不存在', not_active: '已归档或被替代',
  source_snapshot_mismatch: '绑定原话与保存的内容不一致',
  empty_content: '没有可用文字', locked_or_unavailable: '未解锁或不可读取',
  wrong_source_type: '材料类型不匹配', upload_blob_mismatch: '上传文件内容不完整',
  extraction_failed: '文件文字提取失败', unsupported_type: '文件类型不支持提取文字',
};
const types = { event: 'Event', scene: 'Scene', diary: '日记', darkroom: '暗房', upload: '文件' };

export function narrativeErrorMessage(payload, status, action) {
  const code = payload?.reason || payload?.error || '';
  const detail = typeof payload?.detail === 'string' ? payload.detail : '';
  const knownDetail = detail === 'Memory storage unavailable' ? reasons.storage_unavailable
    : detail === 'Authentication required' ? '登录或服务凭据已失效，请重新登录；持续失败时需检查服务凭据。' : '';
  const message = payload?.message || reasons[code] || knownDetail
    || (/^invalid_(event|scene|diary|darkroom|upload)_id$/.test(code) ? '材料编号无效，请重新选择材料。' : '')
    || (status === 401 || status === 403 ? '登录或服务凭据已失效，请重新登录。'
      : status === 413 ? '提交内容过大，请减少材料或缩短正文。'
      : status === 504 ? '等待服务响应超时，请稍后重试。'
      : status === 503 ? '服务暂不可用，请稍后重试。'
      : status === 429 ? '请求过于频繁，请稍后重试。'
      : '服务未能完成请求，请稍后重试。');
  const sources = (payload?.errors || []).map(item =>
    `${types[item.source_type] || '材料'} ${item.source_id}：${sourceReasons[item.reason] || '暂不可读取'}`);
  return [`${action}失败：${message}`, ...sources,
    status ? `HTTP ${status}${code ? ` · ${code}` : ''}` : ''].filter(Boolean).join('\n');
}

export async function narrativeRequest(url, options, action, accepted) {
  let response;
  try { response = await fetch(url, options); }
  catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new Error(`${action}时网络中断，尚未确认结果。请恢复连接后重试。`);
  }
  let payload;
  try { payload = await response.json(); }
  catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new Error(`${action}失败：服务返回了无法读取的响应（HTTP ${response.status}），尚未确认结果，请重试。`);
  }
  if (!response.ok || !payload || (accepted && !accepted.includes(payload.status))) {
    const error = new Error(narrativeErrorMessage(payload, response.status, action));
    error.payload = payload;
    throw error;
  }
  return { response, payload };
}

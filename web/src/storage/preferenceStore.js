async function request(path = '', options = {}) {
  const response = await fetch(`/__serein/personal${path}`, { cache: 'no-store', ...options });
  const result = await response.json();
  if (!response.ok) throw new Error(response.status === 409
    ? '这条喜好已在别处更新。草稿已保留，请取消编辑，刷新后重新打开。'
    : typeof result.detail === 'string' ? result.detail : '喜好暂时没有保存到服务器，请重试。');
  return result;
}

export async function loadPreferences(signal) {
  const rows = [];
  for (let offset = 0; ;) {
    const page = await request(`?scope=preference&offset=${offset}`, { signal });
    rows.push(...page.items);
    if (!page.has_more) break;
    offset = page.next_offset;
  }
  return rows.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

export function savePreference(row, value, deleted = false) {
  return request('', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scope: 'preference', key: row?.key || `pref_${crypto.randomUUID()}`,
      value, expected_revision: row?.revision || 0, deleted }),
  });
}

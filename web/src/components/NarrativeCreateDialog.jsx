import { useEffect, useRef, useState } from "react";
import { X } from "@phosphor-icons/react";
import { createUuid } from "../utils/createUuid.js";
import { narrativeRequest } from '../storage/narrativeErrors.js';

async function request(action, body) {
  const { payload } = await narrativeRequest(`/__serein/narrative-${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }, action === 'create-line' ? '保存叙事线' : '寻找材料',
  action === 'create-line' ? ['created', 'idempotent'] : ['ok']);
  return payload;
}

export function NarrativeCreateDialog({ onClose, onCreated }) {
  const dialog = useRef(null);
  const [theme, setTheme] = useState("");
  const [title, setTitle] = useState("");
  const [result, setResult] = useState(null);
  const [sources, setSources] = useState([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const requestId = useRef(null);
  if (requestId.current === null) requestId.current = createUuid();
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => node.close();
  }, []);
  const search = async () => {
    setBusy("search"); setError(""); setResult(null); setSources([]);
    requestId.current = createUuid();
    try {
      const found = await request("discover-theme", { theme });
      setResult(found); setSources(found.sources || []); setTitle(found.title || "");
    } catch (err) { setError(err.message); }
    finally { setBusy(""); }
  };
  const create = async (write) => {
    setBusy(write ? "write" : "save"); setError("");
    try {
      const saved = await request("create-line", { request_id: requestId.current, theme, title, sources });
      await onCreated(saved.narrative_id, write, saved.narrative ? { ...saved.narrative, source_ledger: sources } : null);
    } catch (err) { setError(err.message); setBusy(""); }
  };
  return <dialog ref={dialog} className="narrative-create" aria-labelledby="narrative-create-title"
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
    <header><div><span className="narrative-kicker">A NEW THREAD</span><h2 id="narrative-create-title">从一个主题开始</h2></div>
      <button type="button" aria-label="关闭新建叙事卷" disabled={Boolean(busy)} onClick={onClose}><X size={20} /></button></header>
    <label>想写什么<textarea autoFocus maxLength={500} value={theme} disabled={Boolean(busy)}
      placeholder="比如：我们怎样一点点建起这个家"
      onChange={(event) => { setTheme(event.target.value); setResult(null); setSources([]); }} /></label>
    <p className="narrative-create__hint">从 Event、日记和 Scene 找材料，先看它们能连成怎样一条线。</p>
    <button type="button" disabled={Boolean(busy) || !theme.trim()} onClick={search}>
      {busy === "search" ? "正在寻找、整理材料…" : result ? "重新找材料" : "找材料"}</button>
    {result && <section className="narrative-create__result">
      <label>书名 <small>{Array.from(title).length}/16</small><input value={title} disabled={Boolean(busy)}
        onChange={(event) => { setTitle(Array.from(event.target.value).slice(0, 16).join("")); requestId.current = createUuid(); }} /></label>
      <p>{result.outline}</p>
      <p className="narrative-create__hint">{sources.length} 条材料 · 按时间排列 · 至少保留 2 条</p>
      {!sources.length && <p>还没找到足够贴合的材料，换一种描述试试。</p>}
      <ol>{sources.map((source) => <li key={`${source.source_type}:${source.source_id}`}>
        <div><small>{source.date?.slice(0, 10) || "日期未记录"} · {{ event: "Event", scene: "Scene", diary: "日记" }[source.source_type]}</small>
          <strong>{source.title}</strong><p>{source.reason}</p></div>
        <button type="button" disabled={Boolean(busy)} aria-label={`移除材料：${source.title}`} onClick={() => {
          setSources(sources.filter((item) => item !== source)); requestId.current = createUuid();
        }}><X size={16} /></button>
      </li>)}</ol>
      {(result.warnings || []).map((warning) => <p key={warning} role="status">{warning}</p>)}
      <footer><button type="button" disabled={Boolean(busy) || sources.length < 2 || !title.trim()} onClick={() => create(false)}>
        {busy === "save" ? "正在保存…" : "保存叙事线"}</button>
        <button type="button" disabled={Boolean(busy) || sources.length < 2 || !title.trim()} onClick={() => create(true)}>
          {busy === "write" ? "准备书写…" : "书写"}</button></footer>
      <p className="narrative-create__hint">保存叙事线会留下一本空白的书，之后可以继续攒材料。</p>
    </section>}
    {error && <p role="alert" style={{ whiteSpace: 'pre-line' }}>{error}</p>}
  </dialog>;
}

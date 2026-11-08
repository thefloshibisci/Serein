import { useEffect, useRef, useState } from "react";
import { authoredType } from "../storage/authoredTypes.js";

const statusNames = { active: "进行中", resolved: "已完成", abandoned: "已放下" };

export function PlanNotes() {
  const [items, setItems] = useState([]), [filter, setFilter] = useState("active");
  const [nextOffset, setNextOffset] = useState(null), [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(""), [error, setError] = useState(false), [draft, setDraft] = useState(null);
  const loading = useRef(null);
  async function load(offset = 0) {
    loading.current?.abort();
    const controller = new AbortController(); loading.current = controller;
    setBusy(true); setMessage(""); setError(false);
    try {
      const result = await authoredType("list_plans", { status: filter, limit: 50, offset }, controller.signal);
      if (controller.signal.aborted) return;
      setItems(previous => offset ? [...previous, ...result.items] : result.items);
      setNextOffset(result.next_offset);
      return true;
    } catch (failure) {
      if (!controller.signal.aborted) { setMessage(failure.message); setError(true); }
      return false;
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  useEffect(() => { setItems([]); setNextOffset(null); load(); return () => loading.current?.abort(); }, [filter]);
  async function open(item) {
    setMessage(""); setError(false);
    if (!item) { setDraft({ content: "", weight: 0.5 }); return; }
    setBusy(true);
    try {
      const current = await authoredType("read_plan", { plan_id: item.id });
      if (!current) throw new Error("这条计划已不存在，请刷新列表。");
      setDraft({ ...current, reason: "" });
    } catch (failure) { setMessage(failure.message); setError(true); }
    finally { setBusy(false); }
  }
  async function save(event) {
    event.preventDefault(); setBusy(true); setMessage(""); setError(false);
    try {
      if (draft.id) await authoredType("update_plan", { plan_id: draft.id, expected_revision: draft.revision,
        content: draft.content, status: draft.status, reason: draft.reason });
      else await authoredType("create_plan", { content: draft.content, weight: Number(draft.weight) });
      setDraft(null);
      if (await load()) setMessage("计划已保存。");
      else setMessage("计划已保存，列表暂未刷新。请刷新确认。");
    } catch (failure) { setMessage(failure.message); setError(true); }
    finally { setBusy(false); }
  }
  return <section className="plan-notes" aria-labelledby="plan-notes-title" aria-busy={busy}>
    <div className="memo-list-heading"><h2 id="plan-notes-title">计划与承诺</h2></div>
    <p className="companion-note">记下想做的事，完成或放下时亲自确认。提醒到期不会替你结束计划。</p>
    <div className="memo-toolbar"><div className="companion-filters" role="group" aria-label="计划状态">
      {Object.entries({ ...statusNames, "": "全部" }).map(([key, label]) => <button key={key} type="button" disabled={busy}
        aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>)}
    </div><div className="companion-actions"><button type="button" className="companion-primary" disabled={busy} onClick={() => open()}>＋ 新增计划</button>
      <button type="button" disabled={busy} onClick={() => load()}>刷新计划</button></div></div>
    {message && <p role={error ? "alert" : "status"}>{message}</p>}
    {draft && <form className="memo-editor" onSubmit={save} aria-label={draft.id ? "编辑计划" : "新增计划"}>
      <h3>{draft.id ? "编辑计划" : "写下计划"}</h3>
      <label className="companion-field">计划正文<textarea autoFocus required rows={4} maxLength={100000} value={draft.content}
        onChange={event => setDraft(current => ({ ...current, content: event.target.value }))} /></label>
      {draft.id ? <><label className="companion-field">计划状态<select value={draft.status}
        onChange={event => setDraft(current => ({ ...current, status: event.target.value }))}>
        {Object.entries(statusNames).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label><label className="companion-field">修改原因<textarea required rows={2} value={draft.reason}
        onChange={event => setDraft(current => ({ ...current, reason: event.target.value }))} /></label></>
        : <label className="companion-field">承诺分量<input type="number" min="0" max="1" step="0.1" required value={draft.weight}
          onChange={event => setDraft(current => ({ ...current, weight: event.target.value }))} /></label>}
      <div className="companion-actions"><button type="submit" className="companion-primary" disabled={busy || !draft.content.trim() || Boolean(draft.id && !draft.reason.trim())}>保存计划</button>
        <button type="button" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
    </form>}
    {!busy && items.length === 0 && <p className="companion-note">这里暂时没有{statusNames[filter] || "保存的"}计划。</p>}
    {items.map(item => <article className={`memo-entry plan-entry--${item.status}`} key={item.id}>
      <div className="memo-title"><h3>{statusNames[item.status]}</h3><span>计划</span></div>
      <p className="memo-body">{item.content}</p>
      <div className="memo-meta">{item.authored_at && <time dateTime={item.authored_at}>{item.authored_at.slice(0, 10)}</time>}
        <span>承诺分量 {item.weight}</span>{item.reminder_id && <span>已关联提醒</span>}</div>
      <details className="plan-history"><summary>变更记录</summary><ol>{(item.history || []).map((entry, index) => <li key={index}>
        {statusNames[entry.to] || "已记录"}{entry.reason ? ` · ${entry.reason}` : ""}{entry.actor ? ` · ${entry.actor}` : ""}
      </li>)}</ol></details>
      <div className="companion-actions"><button type="button" disabled={busy} onClick={() => open(item)}>编辑计划与状态</button></div>
    </article>)}
    {nextOffset !== null && <div className="companion-actions"><button type="button" disabled={busy} onClick={() => load(nextOffset)}>更多计划</button></div>}
  </section>;
}

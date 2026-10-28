import { useEffect, useId, useRef, useState } from 'react';
import { ArrowClockwise, MagnifyingGlass, PencilSimple, Plus, X } from '@phosphor-icons/react';
import { loadPreferences, savePreference } from '../storage/preferenceStore.js';
import './preference-notes.css';

const subjects = ['user', 'ai', 'shared'];
const types = [['scene', 'Scene'], ['event', '事件'], ['preference', '喜好']];
const blank = { subject: 'user', polarity: 'like', content: '', reason: '', keywords: [] };

function PreferenceStamp({ polarity }) {
  const ink = useId();
  const label = polarity === 'like' ? '喜欢' : '不喜欢';
  return <svg className="preference-stamp" viewBox="0 0 166 136" role="img" aria-label={label}>
    <defs><filter id={ink} x="-20%" y="-20%" width="140%" height="140%">
      <feTurbulence type="fractalNoise" baseFrequency=".65" numOctaves="2" seed="7"/>
      <feColorMatrix type="luminanceToAlpha"/>
      <feComponentTransfer><feFuncA type="discrete" tableValues="0 1 1 1"/></feComponentTransfer>
      <feComposite in="SourceGraphic" operator="in"/>
    </filter></defs>
    <g filter={`url(#${ink})`}>
      <g fill="none" stroke="currentColor" strokeWidth="3.5">
        <circle cx="66" cy="68" r="56"/><circle cx="66" cy="68" r="47" strokeWidth="1.8"/>
        {[50, 66, 82].map(y => <path key={y} d={`M118 ${y} c14 -10 24 10 42 0`} strokeWidth="2.5"/>)}
      </g>
      <text x="66" y="36" textAnchor="middle" className="preference-stamp__caption">{polarity.toUpperCase()}</text>
      <rect x="4" y="45" width="124" height="45" rx="4" fill="currentColor"/>
      <rect x="9" y="50" width="114" height="35" rx="2" className="preference-stamp__border"/>
      <text x="66" y="76" textAnchor="middle" className="preference-stamp__label">{label}</text>
      <path d="M49 105h34" stroke="currentColor" strokeWidth="2"/>
    </g>
  </svg>;
}

function Editor({ row, names, onSave, onCancel, busy, error }) {
  const [draft, setDraft] = useState(() => row ? { ...row.value, polarity: row.value.polarity || '' } : blank);
  const [words, setWords] = useState(() => (row?.value.keywords || []).join('、'));
  return <form className="preference-editor" onKeyDown={event => {
    if (event.key === 'Escape' && !busy) { event.preventDefault(); onCancel(); }
  }} onSubmit={event => {
    event.preventDefault();
    const keywords = [...new Set(words.split(/[、,，;；\n]/u).map(word => word.trim()).filter(Boolean))];
    if (!keywords.length) { event.currentTarget.elements.keywords.setCustomValidity('请至少填写一个关键词。'); event.currentTarget.reportValidity(); return; }
    if ([...draft.content].length > 50) { event.currentTarget.elements.content.setCustomValidity('喜好正文最多 50 字，请精简后保存。'); event.currentTarget.reportValidity(); return; }
    onSave(row, { ...draft, content: draft.content.trim(), reason: draft.reason.trim(), keywords });
  }}>
    <fieldset disabled={busy}>
      <legend className="sr-only">{row ? '修改喜好' : '记一条喜好'}</legend>
      <div className="preference-editor__head">
        <label>谁的喜好<select aria-label="谁的喜好" value={draft.subject} onChange={e => setDraft({ ...draft, subject: e.target.value })}>
          {subjects.map(subject => <option key={subject} value={subject}>{names[subject]}</option>)}
        </select></label>
        <button type="button" className="preference-icon" onClick={onCancel} aria-label="取消编辑"><X size={18}/></button>
      </div>
      <label>喜欢／不喜欢<select required aria-label="喜欢／不喜欢" value={draft.polarity} onChange={e => setDraft({ ...draft, polarity: e.target.value })}>
        <option value="" disabled>请选择</option><option value="like">喜欢</option><option value="dislike">不喜欢</option>
      </select></label>
      <label>喜好正文 <span className="preference-editor__count">{[...draft.content].length} / 50</span><textarea name="content" autoFocus required rows={2} placeholder="一条短短的喜好，最多 50 字。" value={draft.content} onChange={e => { e.target.setCustomValidity(''); setDraft({ ...draft, content: e.target.value }); }}/></label>
      <label>原话／原因<textarea required maxLength={1000} rows={2} placeholder="留下一句当时的话，或喜欢它的原因。" value={draft.reason} onChange={e => setDraft({ ...draft, reason: e.target.value })}/></label>
      <label>关键词<input name="keywords" required value={words} onChange={e => { e.target.setCustomValidity(''); setWords(e.target.value); }} placeholder="奶茶、三分糖、甜度" aria-describedby="preference-keyword-hint"/></label>
      <small id="preference-keyword-hint">用顿号或逗号分开。聊到任意一个词，这条喜好就会浮现。</small>
      <div className="preference-editor__actions"><button type="button" onClick={onCancel}>取消</button><button className="preference-save" type="submit">{busy ? '保存中…' : '保存喜好'}</button></div>
    </fieldset>
    {error && <p role="alert" className="preference-error">{error}</p>}
  </form>;
}

export function PreferenceNotes({ onSwitchType, names = { user: '你', ai: 'AI', shared: '我们' } }) {
  const [rows, setRows] = useState([]);
  const [query, setQuery] = useState('');
  const [subject, setSubject] = useState('');
  const [editor, setEditor] = useState(null);
  const [removing, setRemoving] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const sequence = useRef(0);
  const addButton = useRef(null);
  const editButton = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    const current = ++sequence.current;
    setLoading(true); setError('');
    loadPreferences(controller.signal).then(items => {
      if (current === sequence.current) setRows(items);
    }).catch(err => {
      if (err.name !== 'AbortError' && current === sequence.current) setError(err.message);
    }).finally(() => { if (current === sequence.current) setLoading(false); });
    return () => { controller.abort(); sequence.current++; };
  }, [refresh]);

  function closeEditor() {
    setEditor(null); setError('');
    requestAnimationFrame(() => (editButton.current?.isConnected ? editButton.current : addButton.current)?.focus());
  }

  async function save(row, value, deleted = false) {
    setBusy(true); setError(''); setNotice('');
    sequence.current++;
    try {
      const saved = await savePreference(row, value, deleted);
      setRows(current => deleted ? current.filter(item => item.key !== saved.key)
        : [saved, ...current.filter(item => item.key !== saved.key)]);
      closeEditor(); setRemoving(null);
      setNotice(deleted ? '已删除' : '已保存');
    } catch (err) { setError(err.message); }
    finally { setBusy(false); setLoading(false); }
  }

  const needle = query.trim().toLocaleLowerCase();
  const visible = rows.filter(row => (!subject || row.value.subject === subject)
    && (!needle || [row.value.content, row.value.reason, ...row.value.keywords].join('\n').toLocaleLowerCase().includes(needle)));

  return <div className="memory-layout preference-layout">
    <aside className="memory-filters" aria-label="喜好筛选">
      <label className="memory-search"><MagnifyingGlass size={18} aria-hidden="true"/>
        <span className="sr-only">搜索喜好</span><input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索喜好或关键词"/>
        {query && <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}><X size={14}/></button>}
      </label>
      <div className="memory-type-switch" aria-label="记忆类型">
        {types.map(([type, label]) => <button key={type} type="button" aria-pressed={type === 'preference'} className={type === 'preference' ? 'is-active' : ''} onClick={() => type !== 'preference' && onSwitchType(type)}>{label}</button>)}
      </div>
      <div className="memory-filter-group preference-subjects"><h2>谁的喜好</h2>
        {['', ...subjects].map(name => <button type="button" key={name} aria-pressed={subject === name} className={subject === name ? 'is-active' : ''} onClick={() => setSubject(name)}>
          <span className={`preference-dot preference-dot--${subjects.indexOf(name)}`} aria-hidden="true"/><span>{name ? names[name] : '全部'}</span><small>{rows.filter(row => !name || row.value.subject === name).length}</small>
        </button>)}
      </div>
      <p className="preference-rail-note">记得你喜欢什么，<br/>也留得下后来的改变。</p>
    </aside>
    <section className="preference-workspace" aria-labelledby="preference-title">
      <header className="preference-header"><div><p className="preference-eyebrow">a few things we love</p><h1 id="preference-title">喜好</h1><p>喜欢的，不喜欢的，都可以慢慢变。</p></div>
        <div className="preference-header__actions"><button className="preference-icon" type="button" aria-label="刷新喜好" disabled={busy || loading} onClick={() => setRefresh(n => n + 1)}><ArrowClockwise size={18}/></button>
          <button ref={addButton} className="preference-add" type="button" disabled={busy || !!editor || loading} onClick={() => { editButton.current = null; setError(''); setEditor({ key: 'new' }); }}><Plus size={16}/>记一条</button></div>
      </header>
      <div className="preference-notes" aria-busy={loading}>
        {editor?.key === 'new' && <Editor key="new" names={names} onSave={save} onCancel={closeEditor} busy={busy} error={error}/>}
        {error && !editor && <div className="preference-error" role="alert">{error} <button type="button" disabled={busy} onClick={() => setRefresh(n => n + 1)}>重新读取</button></div>}
        {loading && <p className="preference-empty" role="status">正在翻开便签…</p>}
        {!loading && !error && !rows.length && !editor && <div className="preference-empty"><p>还没有写下的喜好。</p><span>从一句「我喜欢……」开始。</span></div>}
        {!loading && rows.length > 0 && !visible.length && <p className="preference-empty">这里还没有符合的喜好。</p>}
        {visible.map(row => editor?.key === row.key ? <Editor key={row.key} row={row} names={names} onSave={save} onCancel={closeEditor} busy={busy} error={error}/>
          : <article key={row.key} className={`preference-note preference-note--${subjects.indexOf(row.value.subject)}`}>
            <header><span>{names[row.value.subject]}</span><time dateTime={row.updated_at}>{new Date(row.updated_at).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric' })}</time>
              <button type="button" className="preference-icon" aria-label={`修改喜好：${row.value.content}`} disabled={busy || !!editor} onClick={event => { editButton.current = event.currentTarget; setEditor(row); setRemoving(null); setError(''); }}><PencilSimple size={17}/></button></header>
            <p className="preference-note__content">{row.value.content}</p>
            <div className="preference-note__detail"><p className="preference-note__reason">{row.value.reason}</p>
              {row.value.polarity && <PreferenceStamp polarity={row.value.polarity}/>}</div>
            <footer><ul aria-label="关键词">{row.value.keywords.map(word => <li key={word}>{word}</li>)}</ul>
              {removing === row.key ? <span className="preference-delete-confirm">删掉这条？<button type="button" disabled={busy} onClick={() => setRemoving(null)}>取消</button><button type="button" disabled={busy} onClick={() => save(row, row.value, true)}>删除</button></span>
                : <button type="button" disabled={busy || !!editor} className="preference-delete" aria-label={`删除喜好：${row.value.content}`} onClick={() => { setRemoving(row.key); setError(''); }}>删除</button>}
            </footer>
          </article>)}
      </div>
      <p role="status" className="preference-notice">{notice}</p>
    </section>
  </div>;
}

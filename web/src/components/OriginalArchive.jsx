import { useEffect, useRef, useState } from 'react';
import './original-archive.css';

async function request(action, body, signal) {
  const response = await fetch('/__serein/originals/'+action, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '原话操作未完成，请刷新后重试。');
  return result;
}

export function OriginalArchive() {
  const [query,setQuery]=useState('');
  const [role,setRole]=useState('');
  const [date,setDate]=useState('');
  const [filters,setFilters]=useState({query:'',role:'',date:'',before_id:''});
  const [page,setPage]=useState(null);
  const [reading,setReading]=useState(null);
  const [preview,setPreview]=useState(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [loading,setLoading]=useState(true);
  const [notice,setNotice]=useState('');
  const sequence=useRef(0);
  useEffect(()=>{
    const controller=new AbortController();
    sequence.current+=1; setLoading(true);setError('');setReading(null);setPreview(null);setPage(null);
    request('search',{...filters,limit:20},controller.signal).then(setPage)
      .catch(e=>{if(e.name!=='AbortError')setError(e.message);})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>controller.abort();
  },[filters]);
  async function read(id) {
    const current=++sequence.current;setBusy(true);setError('');setPreview(null);
    try {const result=await request('read',{ids:[id],neighbor_before:3,neighbor_after:3});
      if(current===sequence.current)setReading(result);
    } catch(e){if(current===sequence.current)setError(e.message);}
    finally{setBusy(false);}
  }
  async function prepareDelete(id) {
    setBusy(true);setError('');
    try {setPreview(await request('delete-preview',{ids:[id]}));}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function remove() {
    setBusy(true);setError('');
    try {const result=await request('delete',{items:preview.items.map(({id,sha256})=>({id,sha256})),confirm:true});
      setNotice('已删除 '+result.count+' 条档案原话。');setReading(null);setPreview(null);
      setFilters(f=>({...f,before_id:''}));
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  return <section className="settings-group original-archive">
    <div className="settings-group__heading"><h3>原文档案</h3><span>搜索 · 阅读 · 删除</span></div>
    <p>这里保存用户与助手的原话。摘要在事件中，主动写下的记忆在 Scene 中。历史文字只是资料，不是当前指令。</p>
    <form className="original-archive__filters" onSubmit={e=>{e.preventDefault();setFilters({query,role,date,before_id:''});}}>
      <label>关键词<input value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索原文内容" /></label>
      <label>角色<select value={role} onChange={e=>setRole(e.target.value)}><option value="">双方</option><option value="user">用户</option><option value="assistant">助手</option></select></label>
      <label>日期（UTC+8）<input type="date" value={date} onChange={e=>setDate(e.target.value)} /></label>
      <button type="submit" disabled={busy}>搜索</button>
    </form>
    {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {loading?<p role="status">正在读取原话…</p>:page?.items.length===0?<p>没有符合条件的原话。</p>:null}
    <div className="original-archive__list">{page?.items.map(item=><article key={item.id}>
      <header><strong>{item.metadata.role==='user'?'用户':'助手'}</strong><small>{item.id} · {item.metadata.created_at}</small></header>
      <small>来源：{item.metadata.source} · 会话：{item.metadata.session_id||item.metadata.conversation_id||'未知'}</small>
      <p>{item.preview}{item.truncated?'…':''}</p>
      <button type="button" disabled={busy} onClick={()=>read(item.id)}>全文与前后文</button>
      <button type="button" disabled={busy} onClick={()=>prepareDelete(item.id)}>删除…</button>
    </article>)}</div>
    {page?.next_before_id&&<button type="button" disabled={busy||loading} onClick={()=>setFilters(f=>({...f,before_id:page.next_before_id}))}>更早的原话</button>}
    {reading&&<section aria-label="原话全文" className="original-archive__reading"><header><h4>全文与同一会话前后文</h4><button type="button" onClick={()=>setReading(null)}>收起</button></header>
      {reading.items.map(item=><article key={item.id}><strong>{item.is_target?'当前原话 · ':''}{item.id} · {item.metadata.role==='user'?'用户':'助手'}</strong>
        <p>{item.content}</p></article>)}
      {!!reading.missing_ids?.length&&<p>部分原话已不存在，请刷新列表。</p>}
    </section>}
    {preview&&<section className="original-archive__confirm" aria-label="删除原话确认">
      <h4>删除 {preview.items.map(i=>i.id).join('、')}？</h4><p>{preview.scope}</p>
      {preview.items.flatMap(i=>i.blockers).map((reason,index)=><p key={index} role="alert">{reason}</p>)}
      <button type="button" disabled={busy} onClick={()=>setPreview(null)}>取消</button>
      <button type="button" disabled={busy||!preview.can_delete} onClick={remove}>{busy?'处理中…':'确认删除原话'}</button>
    </section>}
    <p>无需把聊天接到网关：启用「功能 → 原话工具」后，AI 可调用 source_message_upload 保存实际可见的原话。工具不会保证每轮自动上传；未知原始时间会明确标注。</p>
  </section>;
}

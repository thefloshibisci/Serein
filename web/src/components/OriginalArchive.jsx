import { useEffect, useId, useRef, useState } from 'react';
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
  const readingDialog=useRef(null),readingBody=useRef(null),readTrigger=useRef(null),readController=useRef(null);
  const readingTitle=useId();
  const confirmDialog=useRef(null);
  const deleteTrigger=useRef(null);
  // The confirmation is a modal: a section appended below a long list was
  // off screen, so pressing "删除…" looked like it did nothing.
  useEffect(()=>{
    const node=confirmDialog.current;
    if(!node)return;
    if(preview&&!node.open)node.showModal();
    else if(!preview&&node.open)node.close();
  },[preview]);
  useEffect(()=>{
    const node=readingDialog.current;
    if(reading&&!node.open)node.showModal();
    else if(!reading&&node.open)node.close();
  },[Boolean(reading)]);
  useEffect(()=>{
    const body=readingBody.current,target=body?.querySelector('[data-current-original="true"]');
    if(!body||reading?.loading)return;
    // Scroll only the reader, leaving the archive list and horizontal tabs in place.
    body.scrollTop=target?body.scrollTop+target.getBoundingClientRect().top-body.getBoundingClientRect().top:0;
  },[reading]);
  useEffect(()=>()=>{sequence.current++;readController.current?.abort();},[]);
  useEffect(()=>{
    const controller=new AbortController();
    readController.current?.abort();
    sequence.current+=1; setLoading(true);setError('');setReading(null);setPreview(null);setPage(null);
    request('search',{...filters,limit:20},controller.signal).then(setPage)
      .catch(e=>{if(e.name!=='AbortError')setError(e.message);})
      .finally(()=>{if(!controller.signal.aborted)setLoading(false);});
    return ()=>controller.abort();
  },[filters]);
  function closeReading() {
    sequence.current++;readController.current?.abort();setReading(null);
  }
  async function read(id,trigger) {
    readController.current?.abort();
    const controller=new AbortController();readController.current=controller;readTrigger.current=trigger;
    const current=++sequence.current;setError('');setPreview(null);
    setReading({id,items:[],loading:true,error:''});
    try {const result=await request('read',{ids:[id],neighbor_before:3,neighbor_after:3},controller.signal);
      if(current===sequence.current&&!controller.signal.aborted)setReading({...result,id,loading:false,error:''});
    } catch(e){if(current===sequence.current&&!controller.signal.aborted)setReading({id,items:[],loading:false,error:e.message});}
  }
  async function prepareDelete(id,trigger) {
    deleteTrigger.current=trigger;
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
    {error&&!preview&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {loading?<p role="status">正在读取原话…</p>:page?.items.length===0?<p>没有符合条件的原话。</p>:null}
    <div className="original-archive__list">{page?.items.map(item=><article key={item.id}>
      <header><strong>{item.metadata.role==='user'?'用户':'助手'}</strong><small>{item.id} · {item.metadata.created_at}</small></header>
      <small>来源：{item.metadata.source} · 会话：{item.metadata.session_id||item.metadata.conversation_id||'未知'}</small>
      <p>{item.preview}{item.truncated?'…':''}</p>
      <button type="button" disabled={busy} onClick={event=>read(item.id,event.currentTarget)}>全文与前后文</button>
      <button type="button" disabled={busy} onClick={event=>prepareDelete(item.id,event.currentTarget)}>删除…</button>
    </article>)}</div>
    {page?.next_before_id&&<button type="button" disabled={busy||loading} onClick={()=>setFilters(f=>({...f,before_id:page.next_before_id}))}>更早的原话</button>}
    <dialog ref={readingDialog} className="agent-guide original-archive__reading" aria-labelledby={readingTitle}
      onClose={()=>{closeReading();if(readTrigger.current?.isConnected)readTrigger.current.focus({preventScroll:true});}}>
      <header><h3 id={readingTitle}>全文与同一会话前后文</h3><button type="button" autoFocus onClick={closeReading}>关闭</button></header>
      <div className="original-archive__reading-body" ref={readingBody} aria-busy={reading?.loading||false}>
        {reading?.loading&&<p role="status">正在读取全文与前后文…</p>}
        {reading?.error&&<><p role="alert">{reading.error}</p><button type="button" onClick={()=>read(reading.id,readTrigger.current)}>重新读取</button></>}
        {reading?.items.map(item=><article key={item.id} data-current-original={item.is_target?'true':'false'}>
          <strong>{item.is_target?'当前原话 · ':''}{item.id} · {item.metadata.role==='user'?'用户':'助手'}</strong>
          <p>{item.content}</p></article>)}
        {!!reading?.missing_ids?.length&&<p>部分原话已不存在，请刷新列表。</p>}
      </div>
    </dialog>
    <dialog ref={confirmDialog} className="agent-guide original-archive__confirm" aria-labelledby="original-archive-delete-title" aria-describedby="original-archive-delete-scope"
      onClose={()=>{setPreview(null);if(deleteTrigger.current?.isConnected)deleteTrigger.current.focus({preventScroll:true});}}
      onCancel={event=>{if(busy)event.preventDefault();}}>
      {preview&&<>
        <h3 id="original-archive-delete-title">删除 {preview.items.map(i=>i.id).join('、')}？</h3><p id="original-archive-delete-scope">{preview.scope}</p>
        {preview.items.flatMap(i=>i.blockers).map((reason,index)=><p key={index} role="alert">{reason}</p>)}
        {error&&<p role="alert">{error}</p>}
        <div className="original-archive__confirm-actions">
          <button type="button" autoFocus disabled={busy} onClick={()=>setPreview(null)}>取消</button>
          <button type="button" className="is-danger" disabled={busy||!preview.can_delete} onClick={remove}>{busy?'处理中…':'确认删除原话'}</button>
        </div>
      </>}
    </dialog>
    <p>无需把聊天接到网关：启用「功能 → 原话工具」后，AI 可调用 source_message_upload 保存实际可见的原话。工具不会保证每轮自动上传；未知原始时间会明确标注。</p>
  </section>;
}

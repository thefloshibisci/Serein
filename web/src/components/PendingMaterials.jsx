import {useEffect,useId,useRef,useState} from 'react';
import {formatSourceTime} from '../utils/sourceTime.js';
import {useVisiblePolling} from '../hooks/useVisiblePolling.js';

const roleNames={user:'你',assistant:'AI',tool:'工具',system:'系统'};
const stageName=role=>({track_router:'归线',event_curator:'切分',event_writer:'Event 写作'}[role?.split(':')[0]]||role||'尚未调用模型');
const dateLabel=value=>formatSourceTime(value,{includeYear:true});

export function PendingMaterials({work,onChange,onStatus,id,active=true}) {
  const [items,setItems]=useState([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),[detail,setDetail]=useState(null);
  const [preview,setPreview]=useState(null),[limit,setLimit]=useState(5000),[notice,setNotice]=useState('');
  const [observedWork,setObservedWork]=useState(null),[loadingId,setLoadingId]=useState(null),[focusRaw,setFocusRaw]=useState(null);
  const readSequence=useRef(0),material=useRef(null),finishDialog=useRef(null),finishTrigger=useRef(null);
  const finishTitle=useId();
  // A response already in flight before a confirmed finish cannot reopen it.
  const confirmedFinishes=useRef(new Map());
  const acceptedVersion=useRef(0);
  const running=['running','queued'].includes((work??observedWork)?.status);
  async function call(path,body){
    const response=await fetch('/__serein/pipeline/'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result=await response.json();if(!response.ok)throw new Error(result.detail||'材料暂未读取，请稍后重试。');return result;
  }
  function projected(result){return {...result,pending_materials:(result?.pending_materials||[]).map(item=>({...item,...confirmedFinishes.current.get(item.id)}))};}
  function accept(result){const current=projected(result);acceptedVersion.current++;setItems(current.pending_materials);setObservedWork(current);onStatus?.(current);return current;}
  useEffect(()=>{
    if(work!==undefined){const current=projected(work);acceptedVersion.current++;setItems(current.pending_materials);setObservedWork(current);return;}
  },[work]);
  useVisiblePolling(active&&work===undefined,3000,async current=>{
    const version=acceptedVersion.current;
    try{const result=await call('status');if(current()&&version===acceptedVersion.current)accept(result);}
    catch(e){if(current()&&version===acceptedVersion.current)setError(e.message);}
  });
  useEffect(()=>()=>{readSequence.current++;},[]);
  useEffect(()=>{
    // MCP or another browser can finish the item while this reader is open.
    // The pending page must not retain its old detail or confirmation dialog.
    const stillPending=id=>items.some(item=>item.id===id&&['open','running'].includes(item.status));
    if(detail&&!stillPending(detail.id)){readSequence.current++;setDetail(null);}
    if(loadingId!==null&&!stillPending(loadingId)){readSequence.current++;setLoadingId(null);}
    if(preview&&!stillPending(preview.pending_id))setPreview(null);
  },[items,detail,loadingId,preview]);
  useEffect(()=>{
    const dialog=finishDialog.current;
    if(!dialog)return;
    if(['skip','archive'].includes(preview?.action)&&!dialog.open)dialog.showModal();
    else if(!['skip','archive'].includes(preview?.action)&&dialog.open)dialog.close();
  },[preview]);
  useEffect(()=>{
    if(!detail||!material.current)return;
    const target=focusRaw===null?material.current:[...material.current.querySelectorAll('[data-raw-id]')].find(node=>node.dataset.rawId===String(focusRaw));
    if(!target)return;
    const full=target.querySelector('details');if(full)full.open=true;
    // Scroll only the reading pane/current settings page, never the horizontal
    // settings carousel or the document underneath it.
    let anchor=target;
    for(let node=target.parentElement;node;node=node.parentElement){
      if(!node.matches('.pending-originals,[role="tabpanel"]'))continue;
      const bounds=anchor.getBoundingClientRect(),frame=node.getBoundingClientRect();
      if(bounds.top<frame.top||bounds.bottom>frame.bottom)node.scrollTop+=bounds.top-frame.top-12;
      anchor=node;
    }
    target.focus({preventScroll:true});
  },[detail,focusRaw]);
  async function read(id,rawId=null){
    const sequence=++readSequence.current;setLoadingId(id);setDetail(null);setError('');
    try{const result=await call('pending/'+id);if(sequence===readSequence.current){setFocusRaw(rawId);setDetail(result);}}
    catch(e){if(sequence===readSequence.current)setError(e.message);}
    finally{if(sequence===readSequence.current)setLoadingId(null);}
  }
  async function continueWork(message){
    setNotice(message);setDetail(null);
    try{await call('next',{include_recent:true});}
    catch(e){setNotice(message+' 后续整理未启动：'+e.message);}
    const status=accept(await call('status'));onChange?.(status);
  }
  async function resume(id,replan=false){
    if(busy||running)return;setBusy(true);setError('');setNotice('');
    try{
      const result=await call('resume-pending',{pending_id:id,replan});
      if(result.status!=='resumed')throw new Error('整理正在运行，请稍后再试。');
      await continueWork(replan?'已重建这批材料的整理方案，旧记录保留。':'已恢复这批材料，成功阶段保留。');
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function previewRecovery(item,action='split',trigger=null){
    if(busy||running)return;setBusy(true);setError('');
    finishTrigger.current=trigger;
    try{setPreview({...await call('recovery-preview',{batch_id:item.batch_id,...(action==='skip'?{}:{max_input_chars:Number(limit)})}),pending_id:item.id,action});}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function finishedWithoutMoreWork(pendingId,status,message,extra={}){
    confirmedFinishes.current.set(pendingId,{status,...extra});readSequence.current++;
    setPreview(null);setDetail(null);setNotice(message);
    const updated={...(work??observedWork??{}),pending_materials:items.map(item=>item.id===pendingId?{...item,status,...extra}:item)};
    const accepted=accept(updated);onChange?.(accepted);
    try{const current=accept(await call('status'));onChange?.(current);}
    catch{setNotice(message+' 列表同步暂未完成，可刷新查看。');}
  }
  async function archive(){
    if(busy||running||preview?.action!=='archive')return;setBusy(true);setError('');setNotice('');
    try{
      const result=await call('archive-pending',{pending_id:preview.pending_id,confirm:'ARCHIVE_PENDING_ORIGINALS'});
      if(result.status!=='archived_only')throw new Error('整理正在运行，请稍后再试。');
      await finishedWithoutMoreWork(preview.pending_id,'archived_only','已结束整理，原话保留。',
        {closure_receipt:result.closure_receipt});
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  async function recover(){
    if(busy||running||!preview)return;setBusy(true);setError('');setNotice('');
    try{
      const skip=preview.action==='skip';
      const result=await call(skip?'skip-batch':'rebatch',{batch_id:preview.batch_id,
        confirm:skip?'SKIP_PIPELINE_BATCH':'REBATCH_PIPELINE_BATCH',...(!skip?{max_input_chars:preview.max_input_chars}:{})});
      if(result.status!==(skip?'skipped_batch':'rebatched'))throw new Error('整理正在运行，请稍后再试。');
      if(skip)await finishedWithoutMoreWork(preview.pending_id,'resolved_skipped',`已结束这批 ${result.skipped_originals} 条原话的整理，原话保留。`);
      else{setPreview(null);await continueWork(`已拆成 ${result.batches} 批，旧计划与模型结果保留。`);}
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const open=items.filter(item=>['open','running'].includes(item.status));
  const lastOutput=detail?.last_error||detail?.last_attempt;
  return <section id={id} className="settings-group pending-materials" aria-label="待处理材料" tabIndex={-1}><div className="settings-group__heading"><h3>待处理材料</h3>
    <p>这里只显示尚未结束的材料。可以在这里处理，也可以让已连接 MCP 的助手核对并处理。不需要整理的片段选择“只保留原话”，结束后移出待办，原话仍在原文档案中。</p></div>
    {!open.length&&<p>暂无待处理材料。</p>}
    {open.length>0&&<p className="pending-materials__notice">有 {open.length} 批材料待处理。</p>}
    {open.map(item=><article key={item.id} className="pending-material" aria-label={`待办 ${item.id}`}><h4>待办 {item.id} · {item.kind==='writing'?'已切分待补写':'待归线／切分修复'}</h4>
      <p>{item.summary||'归线尚未完成'} · {item.originals} 条原话 · {item.original_chars.toLocaleString()} 字符</p>
      {item.locator?.first_raw_id!==undefined&&<>
        <p className="pending-material__meta">{item.locator.timestamp_source==='import_time'?'导入记录时间：':''}{dateLabel(item.locator.first_at)} 至 {dateLabel(item.locator.last_at)}（北京时间）<br/>
          来源：{item.locator.source||'未记录'} · 会话：{item.locator.session_id||'未记录'}<br/>
          首条 raw:{item.locator.first_raw_id} · 末条 raw:{item.locator.last_raw_id}</p>
        {item.locator.preview&&<blockquote className="pending-material__excerpt">{item.locator.preview}</blockquote>}
        {item.locator.longest_message&&<p className="pending-material__meta">最长原话：{roleNames[item.locator.longest_message.role]||item.locator.longest_message.role} · raw:{item.locator.longest_message.id} · {item.locator.longest_message.chars.toLocaleString()} 字符{' '}
          <button type="button" className="settings-link" disabled={loadingId===item.id} onClick={()=>read(item.id,item.locator.longest_message.id)}>定位这条原话</button></p>}
      </>}
      {item.last_attempt&&<p>最后步骤：{stageName(item.last_attempt.role)}{item.last_attempt.attempt?` · 第 ${item.last_attempt.attempt} 次记录`:''} · {item.last_attempt.error||'已返回结果'} · 输出 {item.last_attempt.output_chars} 字符</p>}
      <div className="settings-actions"><button type="button" disabled={loadingId===item.id} onClick={()=>read(item.id)}>{loadingId===item.id?'正在读取原话…':'查看原话与最后输出'}</button>
        {item.plan_compatible!==false&&<button type="button" disabled={busy||running||item.status==='running'} onClick={()=>resume(item.id)}>从失败步骤继续</button>}
        <button type="button" disabled={busy||running||item.status==='running'} onClick={()=>resume(item.id,true)}>{item.kind==='unrouted'?'重新归线':'重新切分，保留归线'}</button>
        <button type="button" disabled={busy||running||item.status==='running'} onClick={()=>previewRecovery(item)}>按更小材料重新整理</button>
        {item.can_archive?<button type="button" disabled={busy||running||item.status==='running'} onClick={event=>{finishTrigger.current=event.currentTarget;setError('');setPreview({action:'archive',pending_id:item.id,originals:item.originals});}}>只保留原话</button>:
          item.can_skip&&<button type="button" disabled={busy||running||item.status==='running'} onClick={event=>previewRecovery(item,'skip',event.currentTarget)}>跳过本批，保留原话</button>}
      </div>
      {item.archive_blocker&&<p className="pending-material__meta">暂不能结束整理：{item.archive_blocker}</p>}
      <p className="pending-material__meta">{item.plan_compatible===false?'这批的旧方案来自更早的程序版本，不能原样继续；重新切分会按当前版本生成新方案，可能再次调用模型。':'继续会保留成功步骤；重新整理会生成新方案，可能再次调用模型。'}</p>
      {preview?.pending_id===item.id&&preview.action==='split'&&<div className="pending-recovery" role="region" aria-label="拆分预览">
          <label className="settings-field"><span>每批原话字符上限</span><input type="number" min="2000" max="100000" value={limit} onChange={event=>setLimit(event.target.value)}/></label>
          <button type="button" disabled={busy||running} onClick={()=>previewRecovery(item)}>更新拆分预览</button><p>保留完整问答，拆成 {preview.batches} 批；其中 {preview.oversized_batches} 批仍超出目标。</p>
          {preview.batch_chars&&<p>各批原话字符数：{preview.batch_chars.map(value=>value.toLocaleString()).join('、')}。</p>}
          {!preview.can_rebatch&&<p>这批材料无法按当前目标拆成更多完整问答；可调整目标，或重新归线后继续。</p>}
        <div className="settings-actions"><button type="button" disabled={busy||running||!preview.can_rebatch||Number(limit)!==preview.max_input_chars} onClick={recover}>确认拆小并整理</button><button type="button" disabled={busy} onClick={()=>setPreview(null)}>取消</button></div>
      </div>}
    </article>)}
    <dialog ref={finishDialog} className="agent-guide pending-finish-dialog" aria-labelledby={finishTitle}
      onClose={()=>{setPreview(null);if(finishTrigger.current?.isConnected)finishTrigger.current.focus({preventScroll:true});}}
      onCancel={event=>{if(busy)event.preventDefault();}}>
      {['skip','archive'].includes(preview?.action)&&<>
        <h3 id={finishTitle}>保留原话，结束整理？</h3>
        <p>这项材料中的 {preview.originals} 条原话仍可查阅。结束后移出待办，不再自动重试生成事件；已有事件保持。</p>
        {error&&<p role="alert">{error}</p>}
        <div className="settings-actions"><button type="button" autoFocus disabled={busy} onClick={()=>setPreview(null)}>取消</button>
          <button type="button" disabled={busy||running} onClick={preview.action==='archive'?archive:recover}>{busy?'正在结束…':'确认结束整理'}</button></div>
      </>}
    </dialog>
    {detail&&<section className="pending-material-detail" ref={material} tabIndex={-1} aria-label={`待办 ${detail.id} 的原话与最后输出`}>
      <header><h4>待办 {detail.id} 的原话与最后输出</h4><button type="button" onClick={()=>{readSequence.current++;setDetail(null);}}>收起材料</button></header>
      <p>{detail.status==='archived_only'?'已结束整理，原话保留。':detail.reason}</p>
      <h5>这一批原话</h5>
      <p className="pending-material__meta">按原话顺序展示，长消息可展开全文。{detail.originals??detail.messages.length} 条 · {detail.original_chars?.toLocaleString()??'—'} 字符。</p>
      <div className="pending-originals">{detail.messages.map((message,index)=><article key={message.id??index} data-raw-id={message.id} tabIndex={-1}>
        <header><strong>{roleNames[message.role]||message.role||'原话'} · raw:{message.id??'—'}</strong><span>{dateLabel(message.created_at)}{message.metadata?.timestamp_source==='import_time'?' · 导入记录时间':''}</span></header>
        {(message.content||'').length>1200?<details><summary>{message.content.slice(0,160)}… · 展开完整原话（{message.content.length.toLocaleString()} 字符）</summary><pre>{message.content}</pre></details>:<pre>{message.content||'（此条没有文字正文）'}</pre>}
      </article>)}</div>
      <h5>{detail.last_error?'最后一次错误输出':'最后一次模型输出'}</h5>
      {lastOutput?<><p>{stageName(lastOutput.role)} · {lastOutput.error||'已返回结果'}{lastOutput.created_at?` · ${dateLabel(lastOutput.created_at)}`:''}</p>
        {lastOutput.output_text?<pre className="pending-output">{lastOutput.output_text}</pre>:<p>这次没有收到模型输出正文，错误记录已保留。</p>}</>:<p>这批还没有模型返回记录。</p>}
      <details className="settings-disclosure"><summary>阶段结果与技术记录</summary><p>批次编号：<code>{detail.batch_id||'未记录'}</code> · 已保留 {detail.jobs?.filter(job=>job.output!==null).length??0} 个阶段结果。</p><pre className="pending-output">{JSON.stringify(detail,null,2)}</pre></details>
    </section>}
    {notice&&<p role="status">{notice}</p>}
    {error&&<p role="status">{error}</p>}
  </section>;
}

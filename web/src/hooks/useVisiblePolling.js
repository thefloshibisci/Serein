import {useEffect,useRef} from 'react';

// One request at a time, only while the selected page is visible. Requests
// already in flight finish normally; callers apply them only if still current.
export function useVisiblePolling(enabled,interval,poll) {
  const callback=useRef(poll),pending=useRef(false),refresh=useRef(null);
  callback.current=poll;
  useEffect(()=>{
    if(!enabled)return;
    let alive=true,generation=0,timer;
    const tick=async()=>{
      if(!alive||document.hidden||pending.current)return;
      pending.current=true;
      const started=generation;
      try{await callback.current(()=>alive&&!document.hidden&&started===generation);}
      finally{
        pending.current=false;
        if(refresh.current&&(!alive||started!==generation))refresh.current();
      }
    };
    const visible=()=>{
      generation++;clearInterval(timer);
      refresh.current=document.hidden?null:tick;
      if(!document.hidden){tick();timer=setInterval(tick,interval);}
    };
    visible();document.addEventListener('visibilitychange',visible);
    return()=>{
      alive=false;clearInterval(timer);
      if(refresh.current===tick)refresh.current=null;
      document.removeEventListener('visibilitychange',visible);
    };
  },[enabled,interval]);
}

"""Explicit background host for the same latest pipeline used by HTTP/MCP."""
import asyncio
import json
import logging
import subprocess
from . import Contributions
from .pipeline import advance, scheduled_advance


def process_pending(settings,command,*,runner=None,batch_size=None):
    # Legacy callers retain the function name, but the old one-shot segmenter is
    # retired. Every runner now receives the frozen role request and full prompt.
    async def invoke(role,request):
        if runner:return runner(request)
        result=await asyncio.to_thread(subprocess.run,command,input=json.dumps(request,ensure_ascii=False),encoding='utf-8',
            capture_output=True,timeout=180,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return json.loads(result.stdout)
    return asyncio.run(advance(settings.database,include_recent=True,runner=invoke if runner or command else None))


def factory(services,options):
    if not services._settings.writable:raise ValueError('event_pipeline requires writable storage')
    if options.get('command'):raise ValueError('Configure the agent through pipeline_next/pipeline_submit; the old one-shot Event command has been retired')
    async def run():
        from ..pipeline_observation import report
        previous=None
        while True:
            try:
                result=await scheduled_advance(services._settings.database)
                if result.get('status')!='busy':
                    previous=report(result,previous,logging.getLogger(__name__))
            except Exception:logging.getLogger(__name__).exception('Event pipeline failed; originals remain pending')
            await asyncio.sleep(30)
    return Contributions(jobs={'event_pipeline':run})

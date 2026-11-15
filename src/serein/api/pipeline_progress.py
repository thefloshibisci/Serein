"""Read-only event pipeline backlog, throughput and ETA."""

from fastapi import APIRouter

from ..extensions.pipeline_progress import progress_report


def routes(settings, auth):
    router = APIRouter(dependencies=auth)

    @router.get('/api/pipeline/progress')
    def progress():
        result=progress_report(settings.database)
        from ..core.store import Store
        import json
        with Store(settings.database,read_only=True) as store:
            row=store.conn.execute("SELECT value_json FROM background_state WHERE name='pipeline_auto_observation'").fetchone()
        return {**result,'automatic':json.loads(row[0]) if row else None}

    return router

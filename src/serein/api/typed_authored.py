"""Explicit authored tools shared by public/private MCP and local HTTP."""
from typing import Literal
from ..core.feelings import Feelings
from ..core.plans import Plans
from ..deployment import read_settings


def tools_for(settings):
    feelings, plans = Feelings(settings.database), Plans(settings.database)

    def read_feel(feel_id: str, revision: int | None = None):
        """Read an independent historical feeling with exact triggering source versions. It is not fact evidence."""
        return feelings.read(feel_id, revision)

    def find_feelings(query: str = '', source_id: str = '', date_from: str = '', date_to: str = '',
                      limit: int = 20, offset: int = 0, include_archived: bool = False):
        """Literal feeling search by topic/source/authored date; explicit lookup ignores auto-surface switches."""
        return feelings.search(query, source_id=source_id, date_from=date_from, date_to=date_to,
                               limit=limit, offset=offset, include_archived=include_archived)

    def read_plan(plan_id: str, revision: int | None = None):
        """Read an intention, its status and append-only history, independently of any reminder."""
        return plans.read(plan_id, revision)

    def list_plans(status: str = '', query: str = '', limit: int = 20, offset: int = 0):
        """Find active/resolved/abandoned intentions; they are never evidence of completed events."""
        return plans.list(status=status, query=query, limit=limit, offset=offset)

    tools = {fn.__name__: fn for fn in (read_feel, find_feelings, read_plan, list_plans)}
    if not settings.writable:
        return tools

    def write_feel(content: str, sources: list[dict], title: str = '', authored_at: str | None = None,
                   valence: float | None = None, arousal: float | None = None, can_surface: bool = False):
        """Create an independent feeling with at least one real Event/Scene or authorized source binding. Sources use kind/id and optional revision/hash. No evidence or source content is created/changed. Auto-surfacing is opt-in. Read before retrying a lost response."""
        return feelings.create(content, sources, author=read_settings(settings.database)['identity']['ai_name'],
            title=title, authored_at=authored_at, valence=valence, arousal=arousal, can_surface=can_surface)

    def edit_feel(feel_id: str, expected_revision: int, content: str | None = None,
                  can_surface: bool | None = None, lifecycle: Literal['active', 'archived', 'deleted'] | None = None):
        """Edit a feeling after reading its revision; original trigger snapshots remain fixed."""
        return feelings.edit(feel_id, expected_revision, content=content, can_surface=can_surface, lifecycle=lifecycle)

    def create_plan(content: str, weight: float = 0.5, related: list[dict] | None = None):
        """Record an active intention, optionally linked to exact memories; does not create a reminder or Event."""
        return plans.create(content, actor=read_settings(settings.database)['identity']['ai_name'], weight=weight, related=related)

    def update_plan(plan_id: str, expected_revision: int, reason: str,
                    status: Literal['active', 'resolved', 'abandoned'] | None = None,
                    content: str | None = None, reminder_id: str | None = None):
        """Explicitly update an intention after reading. Resolve/abandon only when instructed, never infer completion from age or reminder expiry. Reason is retained in history. Empty reminder_id detaches a reminder."""
        return plans.update(plan_id, expected_revision, actor=read_settings(settings.database)['identity']['ai_name'],
                            reason=reason, status=status, content=content, reminder_id=reminder_id)

    tools.update({fn.__name__: fn for fn in (write_feel, edit_feel, create_plan, update_plan)})
    return tools

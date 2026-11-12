"""Explicit authored tools shared by public/private MCP and local HTTP."""
from typing import Literal
from ..core.feelings import Feelings
from ..deployment import read_settings
from ..ingest.ob_archive import list_records, read_record


RETIRED_PLAN_TOOLS = frozenset({'create_plan', 'read_plan', 'list_plans', 'update_plan'})
INTERNAL_TYPED_TOOLS = frozenset({'read_feel', 'find_feelings', 'write_feel', 'edit_feel',
                                  'list_ob_archive', 'read_ob_archive'})


def mcp_tools_for(settings):
    def ob_archive(namespace: str, record_id: str = '', genre: str = '', limit: int = 20, offset: int = 0):
        """Read retained OB historical archives. Omit record_id to page the directory by genre; pass a returned record_id to read it. Do not mix a record ID with directory filters. Locks and instance scope are always enforced; archives never enter fact recall."""
        if record_id:
            if genre or offset or limit != 20:
                raise ValueError('Use either record_id or directory options')
            return read_record(settings.database, namespace, record_id)
        return list_records(settings.database, namespace, genre=genre, limit=limit, offset=offset)
    return {'ob_archive': ob_archive}


def tools_for(settings):
    feelings = Feelings(settings.database)

    def read_feel(feel_id: str, revision: int | None = None):
        """Read an independent historical feeling with exact triggering source versions. It is not fact evidence."""
        return feelings.read(feel_id, revision)

    def find_feelings(query: str = '', source_id: str = '', date_from: str = '', date_to: str = '',
                      limit: int = 20, offset: int = 0, include_archived: bool = False):
        """Literal feeling search by topic/source/authored date; explicit lookup ignores auto-surface switches."""
        return feelings.search(query, source_id=source_id, date_from=date_from, date_to=date_to,
                               limit=limit, offset=offset, include_archived=include_archived)

    def list_ob_archive(namespace: str, genre: str = '', limit: int = 20, offset: int = 0):
        """List retained OB archive identities without bodies. Letters keep locks; You/Them need a reviewed instance scope. Archives never enter fact recall."""
        return list_records(settings.database, namespace, genre=genre, limit=limit, offset=offset)

    def read_ob_archive(namespace: str, record_id: str):
        """Read one retained OB genre as historical material. AI-side permissions are fixed; deleted, human-locked and unbound-scope content stays hidden."""
        return read_record(settings.database, namespace, record_id)

    tools = {fn.__name__: fn for fn in (read_feel, find_feelings, list_ob_archive, read_ob_archive)}
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

    tools.update({fn.__name__: fn for fn in (write_feel, edit_feel)})
    return tools

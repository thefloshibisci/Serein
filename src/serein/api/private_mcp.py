"""Private live tool surface: Germany Scene/Diary capabilities, no public authoring."""
from mcp.types import ToolAnnotations
from ..compat.scenes import Scenes
from ..compat.events import Events
from ..compat.diaries import Diaries
from ..core.personal import Personal
from ..core.preferences import Preferences


def add_tools(server,settings):
    scenes=Scenes(settings.database);diaries=Diaries(settings.database);personal=Personal(settings.database)
    preferences=Preferences(settings.database)

    def write_scene(content:str,cues:list[str],title:str='',date:str='',domain:str='',evidence_refs:list[dict]|None=None):
        """Save an authored Scene and optional verbatim source evidence together. No summarization."""
        return scenes.write(content,cues,title,date,domain,evidence_refs)

    def edit_scene(scene_id:str,expected_updated_at:str,title:str|None=None,content:str|None=None,cues:list[str]|None=None):
        """Edit a Scene after reading its current updated_at; conflicts never overwrite newer changes."""
        return scenes.edit(scene_id,expected_updated_at,title=title,content=content,cues=cues)

    def set_scene_status(scene_id:str,expected_updated_at:str,status:str):
        """Set a Scene active or archived, with its current updated_at. Deleted is a retained soft deletion."""
        return scenes.edit(scene_id,expected_updated_at,status=status)

    def edit_event(event_id:str,expected_updated_at:str,title:str|None=None,content:str|None=None):
        """Correct an active Event using its ID and updated_at from read_memory. Returns a new event_id, retaining the old version and original evidence. Omitted title/content stay unchanged; stale edits conflict."""
        return Events(settings.database).edit(event_id,expected_updated_at,title=title,content=content)

    def read_diary(diary_id:int|None=None,date:str='',limit:int=20):
        """Read diaries by ID or date, respecting locked/deleted entries."""
        return diaries.read(diary_id=diary_id,date=date,limit=limit)

    def write_diary(content:str,date:str='',title:str='',unlock_at:str=''):
        """Write an AI-authored diary or sealed diary in the shared notebook."""
        return diaries.create(content=content,date=date,title=title,author='ai',unlock_at=unlock_at)

    def revise_diary(diary_id:int,content:str,title:str|None=None,date:str|None=None):
        """Revise an existing readable diary; retain authorship and previous versions."""
        return diaries.revise(diary_id,content=content,title=title,date=date)

    def delete_diary(diary_id:int):
        """Soft-delete the explicitly selected diary."""
        return diaries.delete(diary_id)

    def comment_diary(diary_id:int,content:str,author:str='ai'):
        """Persist an authored comment in the shared canonical notebook."""
        return diaries.comment(diary_id,content=content,author=author)

    def read_favorites(limit:int=5,offset:int=0,include_archived:bool=False,with_evidence:bool=False):
        """Read the most recently favorited memories. limit=1..100 and offset paginate; deleted memories stay hidden. Explicit reading does not boost or record automatic recall."""
        return personal.read_favorites(limit,offset,include_archived,with_evidence)

    def read_preferences(query:str='',subject:str='',limit:int=50,offset:int=0):
        """Read short preferences by literal text and optional subject role."""
        return preferences.read(query,subject,limit,offset)

    def write_preference(subject:str,polarity:str,content:str,reason:str,keywords:list[str]):
        """Save one short preference with literal match keywords."""
        return preferences.save(subject,content,reason,keywords,polarity=polarity)

    def edit_preference(preference_id:str,expected_revision:int,subject:str,polarity:str,content:str,reason:str,keywords:list[str]):
        """Replace one preference using its current revision."""
        return preferences.save(subject,content,reason,keywords,polarity=polarity,
                                preference_id=preference_id,expected_revision=expected_revision)

    def delete_preference(preference_id:str,expected_revision:int):
        """Delete one preference using its current revision."""
        return preferences.delete(preference_id,expected_revision)

    for fn in (read_diary,read_favorites,read_preferences):
        server.add_tool(fn,annotations=ToolAnnotations(readOnlyHint=True,openWorldHint=False))
    for fn in (write_scene,edit_scene,edit_event,set_scene_status,personal.annotate,write_diary,revise_diary,delete_diary,comment_diary,
               write_preference,edit_preference,delete_preference):
        server.add_tool(fn,annotations=ToolAnnotations(readOnlyHint=False,
            destructiveHint=fn in (set_scene_status,delete_diary,delete_preference),openWorldHint=False))
    tool=server._tool_manager.get_tool('edit_event')
    tool.fn_metadata.arg_model.model_config['extra']='forbid'
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters['additionalProperties']=False

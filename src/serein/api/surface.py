"""Self-use catalog for reusing Germany tools; independent of backend APIs."""

SELF_USE_TOOLS = (
    'recall_memory', 'find_arc', 'read_arc_materials', 'read_memory', 'read_favorites',
    'write_scene', 'edit_scene', 'edit_event', 'set_scene_status', 'annotate',
    'read_diary', 'write_diary', 'revise_diary', 'delete_diary', 'comment_diary',
    'read_preferences', 'write_preference', 'edit_preference', 'delete_preference',
)
BACKEND_ONLY_EVIDENCE = ('bind_scene_evidence', 'unbind_scene_evidence', 'read_scene_evidence')
PUBLIC_AUTHORING_TOOLS = ('narrative_revision_inbox', 'review_narrative_revision', 'publish_narrative')


def self_use_catalog(upstream_tools):
    """Filter an upstream list without changing its backend registration."""
    return [tool for tool in upstream_tools if tool['name'] in SELF_USE_TOOLS]

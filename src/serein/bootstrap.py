"""Initialize only the explicitly configured database and disposable index."""
from .core.store import Store
from .compat.events import Events
from .compat.diaries import Diaries
from .compat.scenes import initialize_scene_ids
from .compat.raw_archive import raw_archive
from .recall.index import build_index


def initialize(settings):
    settings.database.parent.mkdir(parents=True, exist_ok=True)
    with Store(settings.database):
        pass
    Events(settings.database, initialize=True)
    Diaries(settings.database, initialize=True)
    initialize_scene_ids(settings.database)
    raw_archive(settings)
    if settings.index and not settings.index.exists():
        settings.index.parent.mkdir(parents=True, exist_ok=True)
        build_index(settings.database, settings.index)
    return {'status':'initialized','database':str(settings.database),'schema':10}

"""Disposable local retrieval index; canonical state is checked on every query.

No model calls, injection, or writes to the canonical database occur here.
"""

from collections import Counter
import gzip
import json
import math
from pathlib import Path
import re
import sqlite3

from ..core.reader import Reader
from ..core.store import digest, encode


APPLICATION_ID = 0x53524931
PROFILE_KEYS = ("model", "provider_host", "document_instruction", "query_instruction", "max_chars")


def tokens(text):
    """Chinese adjacent pairs and Unicode word runs, not semantic segmentation."""
    result = []
    for word in re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", text.casefold()):
        if "\u3400" <= word[0] <= "\u9fff":
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


def content_stamp(document):
    return digest(encode({key: document[key] for key in
                          ("id", "kind", "revision", "title", "body_md", "metadata")}))


def unit_vector(vector, dimension):
    if (not isinstance(vector, list) or len(vector) != dimension or dimension < 1
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
        raise ValueError("Embedding must contain finite numbers with the declared dimension")
    length = math.hypot(*vector)
    if not length or not math.isfinite(length):
        raise ValueError("Embedding must have a finite nonzero norm")
    return [v / length for v in vector]


def legacy_vector_matches(document, row, profile):
    """Legacy hash wire format only; retrieval does not depend on legacy policy."""
    if not document or document["kind"] != "event" or row["item_type"] != "event":
        return False
    meta = document["metadata"]
    if any(key not in meta for key in ("item_id", "item_type", "fingerprint", "body")):
        return False
    if meta["body"] != document["body_md"] or meta["item_id"] != document["id"]:
        return False
    stamp = {"schema": 1, "profile": {key: profile[key] for key in
                                    ("model", "document_instruction", "max_chars")},
             **{key: meta[key] for key in ("item_id", "item_type", "fingerprint", "body")}}
    expected = digest(json.dumps(stamp, ensure_ascii=False, sort_keys=True))
    return row["source_hash"] == expected and row["model"] == profile["model"]


def build_index(database, index, *, event_cache=None):
    """Build a new independent artifact. Existing files are never overwritten."""
    index = Path(index)
    cache = None
    if event_cache:
        with gzip.open(event_cache, "rt", encoding="utf-8") as file:
            cache = json.load(file)
        profile = {key: cache["profile"][key] for key in PROFILE_KEYS}
    counts = Counter()
    with Reader(database) as reader:
        with index.open("xb"):
            pass
        conn = sqlite3.connect(index)
        try:
            conn.executescript(f"""
                PRAGMA application_id={APPLICATION_ID};
                PRAGMA user_version=1;
                CREATE TABLE documents(id TEXT PRIMARY KEY, kind TEXT NOT NULL, stamp TEXT NOT NULL);
                CREATE VIRTUAL TABLE terms USING fts5(id UNINDEXED, title, body);
                CREATE TABLE vectors(id TEXT PRIMARY KEY, embedding TEXT NOT NULL, dimension INTEGER NOT NULL);
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # All reads in this build refer to the same canonical snapshot.
            reader.store.conn.execute("BEGIN")
            with conn:
                for row in reader.store.conn.execute("SELECT id FROM documents WHERE lifecycle!='deleted' ORDER BY id"):
                    doc = reader.store.read(row["id"])
                    conn.execute("INSERT INTO documents VALUES (?,?,?)", (doc["id"], doc["kind"], content_stamp(doc)))
                    conn.execute("INSERT INTO terms VALUES (?,?,?)",
                                 (doc["id"], " ".join(tokens(doc["title"] or "")), " ".join(tokens(doc["body_md"]))))
                    counts[doc["kind"]] += 1
                if cache:
                    conn.execute("INSERT INTO settings VALUES ('embedding_profile',?)", (encode(profile),))
                    dimension = None
                    for row in cache["event_vectors"]:
                        doc = reader.store.read(row["item_id"])
                        indexed = conn.execute("SELECT 1 FROM documents WHERE id=?", (row["item_id"],)).fetchone()
                        if not indexed or not legacy_vector_matches(doc, row, profile):
                            counts["vectors_rejected"] += 1
                            continue
                        try:
                            vector = unit_vector(json.loads(row["embedding"]), row["dimension"])
                        except (ValueError, TypeError):
                            counts["vectors_rejected"] += 1
                            continue
                        if dimension is not None and dimension != len(vector):
                            raise ValueError("Cache mixes embedding dimensions")
                        dimension = len(vector)
                        conn.execute("INSERT INTO vectors VALUES (?,?,?)", (doc["id"], encode(vector), dimension))
                        counts["event_vectors"] += 1
                    conn.execute("INSERT INTO settings VALUES ('embedding_dimension',?)", (encode(dimension),))
                conn.execute("INSERT INTO settings VALUES ('build_counts',?)", (encode(dict(counts)),))
        finally:
            conn.close()
    return {"index": str(index), "counts": dict(counts), "canonical_writes": 0,
            "remote_embedding_calls": 0}


def refresh_index(database, index, document_ids):
    """Apply committed changes to the disposable index; no canonical writes."""
    with Search(database, index):
        pass
    conn = sqlite3.connect(Path(index).resolve().as_uri() + "?mode=rw", uri=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        optional={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with Reader(database) as reader:
            reader.store.conn.execute("BEGIN")
            for document_id in set(document_ids):
                doc = reader.store.read(document_id)
                old = conn.execute("SELECT stamp FROM documents WHERE id=?", (document_id,)).fetchone()
                if doc and doc["lifecycle"] != "deleted" and old and old[0] == content_stamp(doc):
                    continue
                conn.execute("DELETE FROM terms WHERE id=?", (document_id,))
                conn.execute("DELETE FROM vectors WHERE id=?", (document_id,))
                for table in ('passages','passage_owners','entity_observations'):
                    if table in optional:
                        conn.execute(f'DELETE FROM {table} WHERE document_id=?',(document_id,))
                conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
                if doc and doc["lifecycle"] != "deleted":
                    conn.execute("INSERT INTO documents VALUES (?,?,?)", (doc["id"], doc["kind"], content_stamp(doc)))
                    conn.execute("INSERT INTO terms VALUES (?,?,?)",
                                 (doc["id"], " ".join(tokens(doc["title"])), " ".join(tokens(doc["body_md"]))))
        conn.commit()
    finally:
        conn.close()


class Search:
    def __init__(self, database, index):
        self.reader = Reader(database)
        try:
            self.conn = sqlite3.connect(Path(index).resolve().as_uri() + "?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            if (self.conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                    or self.conn.execute("PRAGMA user_version").fetchone()[0] != 1):
                raise ValueError("Not a supported Serein search index")
            if not self.conn.execute("SELECT 1 FROM settings WHERE key='build_counts'").fetchone():
                raise ValueError("Search index build did not complete")
            tables={row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.has_passages = 'passages' in tables
            self.has_entities = 'entity_observations' in tables
        except Exception:
            if hasattr(self, "conn"):
                self.conn.close()
            self.reader.store.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()
        self.reader.store.close()

    def search(self, query, *, kind=None, mode="surface", limit=10, with_evidence=False,
               query_embedding=None, min_cosine=None, use_passages=False, candidate_ids=None,
               keyword_fallback=False):
        """Return current eligible objects, not proof of successful injection.

        Surface mode permits only current surfaceable Event/Scene. Explicit lookup
        can read archived objects and Narrative when specifically requested.
        Query embeddings require exact query/profile and a caller-chosen cutoff.
        Optional candidate_ids restrict the pool before the result limit; all
        current readability and content-stamp checks still apply.
        """
        if kind not in {None, "scene", "event", "narrative"} or mode not in {"surface", "lookup"}:
            raise ValueError("Unsupported search kind or mode")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        candidate_ids = None if candidate_ids is None else set(candidate_ids)
        if kind == "narrative" and mode != "lookup":
            raise ValueError("Narrative search requires explicit lookup mode")
        query_terms = tokens(query)
        result = {"query": query, "mode": mode, "method": "cosine" if query_embedding is not None else "lexical",
                  "status": "no_match", "items": [], "candidates": 0, "suppressed": {},
                  "injected": False, "semantic_coverage": "indexed_objects_only" if query_embedding is not None else None}
        if not query.strip() or (query_embedding is None and not query_terms):
            return {**result, "status": "empty_or_unsupported_query"}
        if query_embedding is not None:
            stored = self.conn.execute("SELECT value FROM settings WHERE key='embedding_profile'").fetchone()
            if (stored is None or query_embedding.get("profile") != json.loads(stored[0])
                    or query_embedding.get("query") != query):
                raise ValueError("Query embedding must match this query and the complete cached profile")
            if min_cosine is None or not math.isfinite(min_cosine) or not -1 <= min_cosine <= 1:
                raise ValueError("Vector queries require an explicit min_cosine in [-1,1]; it is not a confidence score")
            dimension = json.loads(self.conn.execute("SELECT value FROM settings WHERE key='embedding_dimension'").fetchone()[0])
            if dimension is None:
                return result
            vector = unit_vector(query_embedding["embedding"], dimension)
            merged = {}
            for row in self.conn.execute("SELECT d.*,v.embedding FROM vectors v JOIN documents d ON d.id=v.id"):
                if kind and row['kind'] != kind: continue
                score = sum(a * b for a, b in zip(vector, json.loads(row["embedding"])))
                merged[row['id']]={**dict(row),'score':score,'score_channels':{'whole':score}}
            if use_passages and self.has_passages:
                rows=self.conn.execute('SELECT d.*,p.ordinal,p.start_offset,p.end_offset,p.embedding,p.stamp AS passage_stamp FROM passages p JOIN documents d ON d.id=p.document_id WHERE p.embedding IS NOT NULL')
                for row in rows:
                    if (kind and row['kind'] != kind) or row['stamp'] != row['passage_stamp']: continue
                    score=sum(a*b for a,b in zip(vector,json.loads(row['embedding'])))
                    previous=merged.setdefault(row['id'],{**dict(row),'score':score,'score_channels':{}})
                    if score > previous['score_channels'].get('passage',-2):
                        previous['score_channels']['passage']=score
                        previous['passage']={'ordinal':row['ordinal'],'start_offset':row['start_offset'],'end_offset':row['end_offset']}
                    previous['score']=max(previous['score'],score)
            candidates=[row for row in merged.values() if row['score']>=min_cosine]
            candidates.sort(key=lambda row: (-row["score"], row["id"]))
        else:
            expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in query_terms)
            candidates = self.conn.execute(
                "SELECT d.*, -bm25(terms,0,3,1) AS score FROM terms JOIN documents d ON d.id=terms.id "
                "WHERE terms MATCH ? ORDER BY bm25(terms,0,3,1),d.id", (expression,)).fetchall()
        suppressed = Counter()
        canonical = self.reader.store.conn
        canonical.execute("BEGIN")
        try:
            for row in candidates:
                if candidate_ids is not None and row['id'] not in candidate_ids:
                    continue
                if row["kind"] not in ({kind} if kind else {"event", "scene"}):
                    continue
                result["candidates"] += 1
                current = self.reader.read(row["id"], kind=row["kind"], with_evidence=False)
                if not current["readable"]:
                    suppressed[current["status"]] += 1
                    continue
                if mode == "surface" and not current["surface_state"]["can_surface"]:
                    suppressed.update(current["surface_state"]["reasons"])
                    continue
                if content_stamp(current["document"]) != row["stamp"]:
                    suppressed["stale_content_rebuild_required"] += 1
                    continue
                if len(result["items"]) < limit:
                    if with_evidence:
                        current = self.reader.read(row["id"], kind=row["kind"], with_evidence=True)
                    hit={"id": row["id"], "kind": row["kind"], "score": row["score"], "object": current}
                    if query_embedding is not None:
                        hit['score_channels']=row['score_channels']
                        if row.get('passage'): hit['passage']=row['passage']
                    result["items"].append(hit)
        finally:
            canonical.rollback()
        result["suppressed"] = dict(suppressed)
        result["status"] = "matched" if result["items"] else "no_match"
        # Explicit manual lookup may contain alternative names/topics. Keep
        # strict matches first; only broaden an empty eligible result, never
        # an automatic surface request. Each keyword retains all its tokens.
        keywords=list(dict.fromkeys(re.split(r'[\s,，、;；]+', query.strip())))
        keywords=[word for word in keywords if tokens(word)]
        if keyword_fallback and query_embedding is None and not result['items'] and len(keywords)>1:
            if len(keywords)>12:
                raise ValueError('Use at most 12 separated memory keywords')
            found, matches, ranks = {}, {}, Counter()
            for keyword in keywords:
                page=self.search(keyword,kind=kind,mode=mode,limit=100,
                                 with_evidence=with_evidence,candidate_ids=candidate_ids)
                for rank,hit in enumerate(page['items'],1):
                    found[hit['id']]=hit
                    matches.setdefault(hit['id'],[]).append(keyword)
                    ranks[hit['id']]+=1/(60+rank)
            ranked=sorted(found,key=lambda key:(-len(matches[key]),-ranks[key],key))
            result.update(match_mode='keyword_fallback',candidates=len(found),
                          items=[{**found[key],'match_mode':'keyword_fallback',
                                  'matched_keywords':matches[key]} for key in ranked[:limit]])
            result['status']='matched' if result['items'] else 'no_match'
        return result

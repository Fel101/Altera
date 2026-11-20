"""
Altera — Memory System (Phase 2)

Uses ChromaDB for persistent vector memory. Extracts facts from conversations,
stores them with embeddings, and retrieves relevant memories when generating responses.
All data stays local on your machine.
"""

import logging
import json
import math
import re
import threading
import uuid
import hashlib
from datetime import datetime, UTC

LOGGER = logging.getLogger(__name__)
COLLECTION = "altera_memories"
# Altera was called Her; an install from then keeps its memories in a collection with this name. It is renamed to
# COLLECTION the first time the collection opens, unless COLLECTION already exists.
LEGACY_COLLECTION = "her_memories"


class MemoryStore:
    """ChromaDB-based memory store for Altera."""

    def __init__(self, persist_directory=None):
        if persist_directory is None:
            from core.paths import data_path
            persist_directory = data_path('chroma_db')
        self.persist_directory = str(persist_directory)
        self._init_lock = threading.RLock()
        self._client = None
        self._collection = None

    @property
    def client(self):
        """Lazy-load ChromaDB to avoid import overhead."""
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    import chromadb
                    from chromadb.config import Settings
                    self._client = chromadb.PersistentClient(path=self.persist_directory, settings=Settings(anonymized_telemetry=False))
        return self._client

    @property
    def collection(self):
        """Get or create the memories collection."""
        if self._collection is None:
            with self._init_lock:
                if self._collection is None:
                    from core.local_embeddings import default_collection_options, require_default_compatible
                    names = {getattr(item, "name", item) for item in self.client.list_collections()}
                    if LEGACY_COLLECTION in names and COLLECTION not in names:
                        self.client.get_collection(LEGACY_COLLECTION).modify(name=COLLECTION)
                        LOGGER.info("Memories moved from the old collection name to %s.", COLLECTION)
                    collection = self.client.get_or_create_collection(
                        name=COLLECTION,
                        metadata={"hnsw:space": "cosine"},  # cosine similarity for semantic search
                        **default_collection_options(),
                    )
                    require_default_compatible(collection)
                    self._collection = collection
        return self._collection

    def add_memory(self, content, memory_type="fact", confidence=0.8, session_id=None, user_context=None, avatar_id=None,
                   source_refs=None, extracted_by=None):
        """Add a new memory entry. `avatar_id` says which companion it belongs to: each one only remembers what was said to her.
        `extracted_by` records whether the chat model wrote it ('model') or the phrase matcher stood in ('phrases')."""
        now = datetime.now().isoformat()

        # A whole second of resolution and 10_000 buckets of a per-process-randomised hash() is not enough to keep two
        # memories apart: two facts extracted from the same message could collide, and ChromaDB would silently keep
        # only the last. A uuid cannot.
        mem_id = f"mem_{uuid.uuid4().hex}"

        self.collection.add(
            documents=[content],
            metadatas=[{
                "type": memory_type,
                "confidence": confidence,
                "session_id": session_id or "",
                "avatar_id": avatar_id or "",
                "user_context": user_context or "",
                "source_refs": json.dumps(source_refs or []),
                "extracted_by": extracted_by or "",
                "created_at": now,
                "updated_at": now,
                "salience": 3,
                "pinned": False,
                "priority_revision": 0,
                "salience_anchor_at": datetime.now(UTC).isoformat(),
                "id": mem_id
            }],
            ids=[mem_id]
        )

        return mem_id

    def search(self, query, limit=5, avatar_id=None):
        """Search memories by semantic similarity. With `avatar_id`, only that companion's memories are searched."""
        count = getattr(self.collection, 'count', None)
        if callable(count):
            limit = min(limit, count())
        if limit <= 0:
            return []
        results = self.collection.query(
            query_texts=[query],
            n_results=limit,
            where={"avatar_id": avatar_id} if avatar_id is not None else None
        )

        memories = []
        for i in range(len(results["ids"][0])):
            memories.append({
                "id": results["ids"][0][i],
                "content": results["documents"][0][i],
                "metadata": results["metadatas"][0][i] or {},
                "distance": results["distances"][0][i] if results.get("distances") else None,
                "similarity": 1.0 - (results["distances"][0][i] / 2.0) if results.get("distances") else None
            })

        return memories

    def pinned(self, avatar_id, limit=20):
        """Bounded metadata-only candidates; never run an embedding to list pins."""
        raw = self.collection.get(where={'$and': [{'avatar_id': avatar_id or ''}, {'pinned': True}]}, limit=limit)
        return [{'id': identifier, 'content': content or '', 'metadata': metadata or {}}
                for identifier, content, metadata in zip(raw.get('ids', []), raw.get('documents', []), raw.get('metadatas', []))
                if (metadata or {}).get('pinned') is True and ((metadata or {}).get('avatar_id') or '') == (avatar_id or '')][:limit]

    def get_all(self, avatar_id=None):
        """Get all memories (or only one companion's)."""
        results = self.collection.get(where={"avatar_id": avatar_id}) if avatar_id else self.collection.get()
        memories = []
        for i in range(len(results["ids"])):
            memories.append({
                "id": results["ids"][i],
                "content": results["documents"][i] if results.get("documents") else "",
                "metadata": (results["metadatas"][i] or {}) if results.get("metadatas") else {}
            })
        return [m for m in memories if avatar_id is None or ((m.get('metadata') or {}).get('avatar_id') or '') == avatar_id]

    def update_memory(self, memory_id, content=None, confidence=None, extracted_by=None):
        """Update an existing memory."""
        metadata_updates = {}
        if confidence is not None:
            metadata_updates["confidence"] = confidence
        if extracted_by is not None:
            metadata_updates["extracted_by"] = extracted_by
        metadata_updates["updated_at"] = datetime.now().isoformat()

        old = self.collection.get(ids=[memory_id])
        if not old or not old.get("ids"):
            return
        kwargs = {"ids": [memory_id], "metadatas": [{**old["metadatas"][0], **metadata_updates}]}
        if content is not None:
            kwargs["documents"] = [content]
        # Chroma updates documents and embeddings in place. Never delete a memory before its replacement succeeds.
        self.collection.update(**kwargs)

    def delete_memory(self, memory_id):
        """Delete a memory."""
        self.collection.delete(ids=[memory_id])

    def assign_unowned(self, avatar_id):
        """Give every memory that belongs to nobody yet (made before companions existed) to one companion. Returns how many."""
        results = self.collection.get()
        ids, metas = [], []
        for memory_id, meta in zip(results.get("ids") or [], results.get("metadatas") or []):
            if not (meta or {}).get("avatar_id"):
                ids.append(memory_id)
                metas.append({**(meta or {}), "avatar_id": avatar_id})
        if ids:
            self.collection.update(ids=ids, metadatas=metas)
        return len(ids)

    def clear_all(self):
        """Clear all memories (use with caution)."""
        # A fresh instance can also clear an empty store; no embedding call is needed.
        from core.local_embeddings import IncompatibleEmbeddingModel
        try:
            self.collection  # noqa: B018 - opening the collection is the check
        except IncompatibleEmbeddingModel:
            # Explicit Forget may delete an unsupported model's stored vectors;
            # refusing semantic substitution must not prevent their removal.
            pass
        self.client.delete_collection(COLLECTION)
        self._collection = None

    def snapshot(self):
        """A portable logical snapshot, including embeddings (no model call on restore)."""
        collection = self.collection
        raw = collection.get(include=["documents", "metadatas", "embeddings"])
        embeddings = raw.get("embeddings")
        if hasattr(embeddings, "tolist"):
            embeddings = embeddings.tolist()
        fn = getattr(collection, "_embedding_function", None)
        name = fn.name() if fn and callable(getattr(fn, "name", None)) else "default"
        records = [{"id": item, "content": raw["documents"][i] or "", "metadata": raw["metadatas"][i],
                    "embedding": embeddings[i]} for i, item in enumerate(raw["ids"])]
        result = {"version": 1, "embedding_function": name, "records": records}
        self.validate_snapshot(result)
        return result

    @staticmethod
    def validate_snapshot(snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or not isinstance(snapshot.get("embedding_function"), str):
            raise ValueError("Unsupported memory snapshot.")
        records = snapshot.get("records")
        if not isinstance(records, list):
            raise ValueError("The memory snapshot has no records.")
        ids, dimension = set(), None
        for row in records:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in ids:
                raise ValueError("Invalid or duplicate memory ID.")
            ids.add(row["id"])
            if not isinstance(row.get("content"), str) or row.get("metadata") is not None and not isinstance(row["metadata"], dict):
                raise ValueError("Invalid memory content or metadata.")
            for value in (row.get("metadata") or {}).values():
                if not isinstance(value, (str, bool, int, float)) or isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("Invalid memory metadata value.")
            vector = row.get("embedding")
            if not isinstance(vector, list) or not vector or len(vector) > 8192:
                raise ValueError("Invalid memory embedding.")
            dimension = dimension or len(vector)
            if len(vector) != dimension or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                raise ValueError("Inconsistent or non-finite memory embeddings.")

    def restore_snapshot(self, snapshot):
        self.validate_snapshot(snapshot)
        existing = self.collection
        fn = getattr(existing, "_embedding_function", None)
        name = fn.name() if fn and callable(getattr(fn, "name", None)) else "default"
        if name != snapshot["embedding_function"]:
            raise ValueError("This backup requires a different memory embedding model.")
        self.client.delete_collection(COLLECTION)
        options = {"name": COLLECTION, "metadata": {"hnsw:space": "cosine"}}
        if name == 'default':
            from core.local_embeddings import default_collection_options
            options.update(default_collection_options(embedding_function=fn))
        elif fn is not None:
            options["embedding_function"] = fn
        self._collection = self.client.get_or_create_collection(**options)
        for has_metadata in (True, False):
            rows = [r for r in snapshot["records"] if bool(r.get("metadata")) == has_metadata]
            for offset in range(0, len(rows), 500):
                group = rows[offset:offset + 500]
                kwargs = {"ids": [r["id"] for r in group], "documents": [r["content"] for r in group],
                          "embeddings": [r["embedding"] for r in group]}
                if has_metadata:
                    kwargs["metadatas"] = [r["metadata"] for r in group]
                self.collection.add(**kwargs)


class MemoryExtractor:
    """The phrase matcher, used only when the chat model cannot write memories.

    It is deliberately simple and word-boundary based: "I'm a bit scared of the sea" is not a fact about who someone
    is, and it used to be stored as one because "i'm a" appeared inside it. What it finds is marked in the stored
    memory as written by phrases (`extracted_by`), so the Memory page can say where a fact came from.
    """

    # Words that turn "I'm a" / "I have a" into a feeling or a guess rather than a fact about the person.
    _NOT_A_FACT = re.compile(r"\s+(?:bit|little|lot|tad|touch|feeling|hunch|suspicion|memory|dream)\b")

    # Keywords that indicate important information
    FACT_INDICATORS = [
        "i work at", "i'm a", "i live in", "i grew up in",
        "my favorite", "i love", "i hate", "i enjoy",
        "i have a", "i went to", "i studied",
        "my name is", "i'm from", "i'm married to",
        "i have kids", "i work with", "i play",
    ]

    PREFERENCE_INDICATORS = [
        "i like", "i prefer", "i don't like", "i can't stand",
        "i always", "i never", "i usually", "i tend to",
        "i'm really into", "i'm obsessed with", "i'm not a fan of",
    ]

    EVENT_INDICATORS = [
        "last week", "yesterday", "next month", "recently",
        "just got", "just finished", "just started",
        "today I", "this morning", "this weekend",
    ]

    @classmethod
    def _says(cls, text_lower, indicator):
        """Whether the phrase occurs as words, and says something about the person rather than about a feeling."""
        match = re.search(r"(?<![a-z])" + re.escape(indicator) + r"(?![a-z])", text_lower)
        if not match:
            return False
        if indicator in ("i'm a", "i am a", "i have a"):
            return not cls._NOT_A_FACT.match(text_lower[match.end():])
        return True

    def extract_from_text(self, text):
        """Extract memories from a piece of text."""
        text_lower = (text or "").lower()
        memories = []
        seen = set()  # avoid duplicates within same extraction

        # Split into sentences for individual analysis
        sentences = [s.strip() for s in (text or '').split('.') if len(s.strip()) > 10]

        for sentence in sentences:
            sent_lower = sentence.lower()
            content_key = sentence.strip().lower()

            # Check for facts
            for indicator in self.FACT_INDICATORS:
                if self._says(sent_lower, indicator) and content_key not in seen:
                    memories.append({
                        "content": sentence,
                        "type": "fact",
                        "confidence": 0.55
                    })
                    seen.add(content_key)
                    break

            # Check for preferences
            if content_key not in seen:
                for indicator in self.PREFERENCE_INDICATORS:
                    if self._says(sent_lower, indicator):
                        memories.append({
                            "content": sentence,
                            "type": "preference",
                            "confidence": 0.6
                        })
                        seen.add(content_key)
                        break

            # Check for events
            if content_key not in seen:
                for indicator in self.EVENT_INDICATORS:
                    if self._says(sent_lower, indicator):
                        memories.append({
                            "content": sentence,
                            "type": "event",
                            "confidence": 0.55
                        })
                        seen.add(content_key)
                        break

        # Also extract using simple pattern matching for names, places, etc.
        name_pattern = r'(my name is|i\'m (?:called|named))\s+(\w+)'
        place_pattern = r'(i live in|i\'m from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)'

        for match in re.finditer(name_pattern, text_lower):
            memories.append({
                "content": f"User's name is {match.group(2).title()}",
                "type": "fact",
                "confidence": 0.95
            })

        for match in re.finditer(place_pattern, text_lower):
            memories.append({
                "content": f"User lives in {match.group(2)}",
                "type": "fact",
                "confidence": 0.85
            })

        return memories


class MemoryDeduplicator:
    """Detect and merge duplicate memories to keep the knowledge base clean."""

    def __init__(self, similarity_threshold=0.85):
        self.threshold = similarity_threshold

    def find_duplicates(self, memories=None):
        """Find groups of similar/duplicate memories across all stored memories.

        Returns list of groups, where each group is a list of memory dicts that are similar.
        Only returns groups with 2+ members (actual duplicates).
        """
        if memories is None:
            mem_mgr = get_memory_manager()
            memories = mem_mgr.get_all_memories()

        # Group by type first to reduce comparisons
        by_type = {}
        for m in memories:
            from core.knowledge import references, can_recall
            from core.datastore import get_datastore
            if references(m.get('metadata')) and not can_recall(m, get_datastore()):
                continue
            # a memory of one companion is never merged with another companion's
            mem_type = (m.get("metadata", {}).get("avatar_id", ""), m.get("metadata", {}).get("type", "fact"))
            if mem_type not in by_type:
                by_type[mem_type] = []
            by_type[mem_type].append(m)

        duplicate_groups = []
        processed = set()  # Track which memory IDs have been grouped

        for mem_type, type_memories in by_type.items():
            # Each memory's text is prepared once here. The comparison itself still looks at every remaining pair --
            # the threshold can be met through the substring rule without the two sharing many words, so there is no
            # cheap way to rule a pair out in advance -- but it used to lowercase and split BOTH memories afresh on
            # every one of those comparisons, which is where nearly all the time went.
            prepared = [(m, str(m.get("content", "")).lower(), set(str(m.get("content", "")).lower().split()))
                        for m in type_memories]

            for index, (memory, text, words) in enumerate(prepared):
                if memory["id"] in processed:
                    continue

                group, scores = [memory], []
                for other_index, (other, other_text, other_words) in enumerate(prepared):
                    if other_index == index or other["id"] in processed:
                        continue
                    score = self._score(text, words, other_text, other_words)
                    if score >= self.threshold:
                        group.append(other)
                        scores.append(score)

                if len(group) >= 2:
                    duplicate_groups.append({
                        "type": mem_type,
                        "memories": group,
                        "similarity_scores": scores,
                    })
                    for member in group:
                        processed.add(member["id"])

        return duplicate_groups

    @staticmethod
    def _score(text1, words1, text2, words2):
        """Word overlap of two already-lowercased, already-split memories, 0..1."""
        if not words1 or not words2:
            return 0.0
        union = len(words1 | words2)
        jaccard = len(words1 & words2) / union if union else 0.0
        if text1 in text2 or text2 in text1:
            return max(jaccard, 0.85)
        return jaccard

    def _calculate_similarity(self, mem1, mem2):
        """How alike two memories are, 0..1, from their text.

        This used to start by averaging mem1["similarity"] and mem2["similarity"] when both were set. Those are each
        memory's similarity to some *earlier, unrelated* search query — set by MemoryStore.search() — so for any two
        memories that arrived from a search the answer had nothing to do with the pair being compared. The word
        overlap below was only ever reached for memories that came from get_all().
        """
        content1 = mem1.get("content", "")
        content2 = mem2.get("content", "")

        if isinstance(content1, str) and isinstance(content2, str):
            words1 = set(content1.lower().split())
            words2 = set(content2.lower().split())

            if not words1 or not words2:
                return 0.0

            intersection = len(words1 & words2)
            union = len(words1 | words2)
            jaccard = intersection / union if union > 0 else 0

            # Also check for substring match (one contains the other)
            if content1.lower() in content2.lower() or content2.lower() in content1.lower():
                return max(jaccard, 0.85)

            return jaccard

        return 0.0

    def consolidate_duplicates(self, duplicate_groups=None):
        """Merge duplicate memories into single high-confidence entries.

        Returns list of consolidation actions taken.
        """
        if duplicate_groups is None:
            duplicate_groups = self.find_duplicates()

        consolidations = []

        for group in duplicate_groups:
            if len(group["memories"]) < 2:
                continue
            owners = {(m.get('metadata') or {}).get('avatar_id') or '' for m in group['memories']}
            if len(owners) != 1 or any((m.get('metadata') or {}).get('user_context') == 'manual_corrected' for m in group['memories']):
                continue  # User corrections cannot be overwritten by an automatic consolidation.
            from core.knowledge import can_recall
            from core.datastore import get_datastore
            if any(not can_recall(m, get_datastore()) for m in group['memories']):
                continue

            # Keep the highest-confidence memory, merge others into it
            memories = group["memories"]
            best = max(memories, key=lambda m: float(m.get("metadata", {}).get("confidence", 0)))

            merged_content = self._merge_contents([m["content"] for m in memories])
            avg_confidence = sum(
                float(m.get("metadata", {}).get("confidence", 0))
                for m in memories
            ) / len(memories)

            # Boost confidence based on number of confirmations
            boosted_confidence = min(avg_confidence + (len(memories) - 1) * 0.05, 0.98)

            try:
                mem_mgr = get_memory_manager()

                # Update the best memory with merged content and boosted confidence
                mem_mgr.store.update_memory(
                    best["id"],
                    content=merged_content,
                    confidence=boosted_confidence
                )
                from core.knowledge import attach_sources, references
                attach_sources(mem_mgr.store, best['id'], [ref for m in memories for ref in references(m.get('metadata'))])
                from core.salience import describe, instant
                ranked = [describe(m.get('metadata')) for m in memories]
                raw = mem_mgr.store.collection.get(ids=[best['id']])
                merged_meta = {**(raw['metadatas'][0] or {}),
                               'pinned': any(p['pinned'] for p in ranked),
                               'salience': max(p['salience'] for p in ranked),
                               'priority_revision': max(p['revision'] for p in ranked) + 1,
                               'recall_count': max(p['recall_count'] for p in ranked)}
                for key in ('last_recalled_at', 'salience_anchor_at'):
                    known = [instant((m.get('metadata') or {}).get(key)) for m in memories]
                    known = [value for value in known if value is not None]
                    if known:
                        merged_meta[key] = max(known).isoformat()
                recalled_ids = [(m.get('metadata') or {}).get('last_recall_message_id') for m in memories]
                merged_meta['last_recall_message_id'] = max([value for value in recalled_ids if type(value) is int and value > 0] or [0])
                mem_mgr.store.collection.update(ids=[best['id']], metadatas=[merged_meta])

                # Delete duplicate memories (except the best one)
                deleted_ids = []
                for m in memories:
                    if m["id"] != best["id"]:
                        mem_mgr.store.delete_memory(m["id"])
                        deleted_ids.append(m["id"])

                consolidations.append({
                    "kept": best["id"],
                    "deleted": deleted_ids,
                    "merged_count": len(memories),
                    "new_confidence": boosted_confidence,
                    "type": group["type"]
                })
            except Exception:
                LOGGER.exception("Consolidation error")

        return consolidations

    def _merge_contents(self, contents):
        """Merge multiple similar memory contents into one coherent entry."""
        if len(contents) == 1:
            return contents[0]

        # Deduplicate sentences within merged content
        seen = set()
        unique_sentences = []

        for content in contents:
            sentences = [s.strip() for s in str(content).split('.') if s.strip()]
            for sent in sentences:
                key = sent.lower().strip()
                if key not in seen:
                    seen.add(key)
                    unique_sentences.append(sent)

        # If we have multiple sentences, combine them intelligently
        if len(unique_sentences) > 1:
            # Check if one sentence contains all the information
            full_text = '. '.join(unique_sentences)
            if any(len(s) >= len(full_text) * 0.7 for s in unique_sentences):
                return max(unique_sentences, key=len)

            return '; '.join(unique_sentences[:3])  # Limit to most important

        return contents[0]


class MemoryManager:
    """High-level memory management for Altera."""

    def __init__(self):
        self.store = MemoryStore()
        self.extractor = MemoryExtractor()
        self.deduplicator = MemoryDeduplicator()

    def add_conversation_memories(self, conversation_text, session_id=None, avatar_id=None, source_refs=None):
        """Extract and store memories with the phrase matcher alone. This is the fallback, not the normal path."""
        found = [{**mem, "refs": source_refs or []} for mem in self.extractor.extract_from_text(conversation_text)]
        return self.store_extracted(found, session_id=session_id, avatar_id=avatar_id, extracted_by="phrases")

    def store_extracted(self, extracted, session_id=None, avatar_id=None, source_refs=None, extracted_by=None):
        """Save facts another pass already wrote (the model, or the phrase matcher), merging repeats."""
        from core import safety
        from core.datastore import get_datastore
        from core.knowledge import attach_sources, can_recall
        ds, stored_ids, owner = get_datastore(), [], avatar_id or ""

        for mem in extracted or []:
            content = str(mem.get("content") or "").strip()
            if not content or safety.blocks_message(content):
                continue
            refs = list(mem.get("refs") or source_refs or [])
            try:
                existing = self.store.search(content, limit=3, avatar_id=owner)
            except Exception:
                LOGGER.exception("Could not check for a similar memory; saving this one as new")
                existing = []
            merged = False
            for ex in existing:
                if not can_recall(ex, ds) or not ex.get("similarity") or ex["similarity"] <= 0.85:
                    continue
                meta = ex.get("metadata") or {}
                current = float(meta.get("confidence", 0))
                # A model-written statement is better evidence than the phrase fallback that stood in earlier.
                upgrade = (extracted_by == "model" and meta.get("user_context") == "auto_extracted"
                           and meta.get("extracted_by") == "phrases")
                self.store.update_memory(ex["id"], confidence=max(current, float(mem.get("confidence") or 0)),
                                         extracted_by="model" if upgrade else None)
                if refs:
                    attach_sources(self.store, ex["id"], refs)
                merged = True
                break
            if merged:
                continue
            stored_ids.append(self.store.add_memory(
                content=content,
                memory_type=mem.get("type") or "fact",
                confidence=float(mem.get("confidence") or 0.8),
                session_id=(refs[0].get("session_id") if refs else session_id),
                user_context="auto_extracted",
                avatar_id=avatar_id,
                source_refs=refs,
                extracted_by=extracted_by,
            ))

        return stored_ids

    def get_relevant_memories(self, query, limit=5, avatar_id=None):
        """Get memories relevant to a given query (only one companion's, when `avatar_id` is given)."""
        from core.datastore import get_datastore
        from core.knowledge import can_recall, dirty
        from core import safety
        owner, ds = avatar_id or '', get_datastore()
        if dirty(ds, owner):
            return []
        from core.salience import describe, lexical_relevance
        limit = max(1, min(20, int(limit)))
        found = self.store.search(query, limit=min(40, limit * 4), avatar_id=owner)
        candidates = {m['id']: m for m in found}
        pinned = getattr(self.store, 'pinned', None)
        for item in pinned(owner, limit=20) if callable(pinned) else []:
            relevance = lexical_relevance(query, item.get('content'))
            if item['id'] not in candidates and relevance > 0:
                candidates[item['id']] = {**item, 'similarity': relevance * .8, 'retrieval_kind': 'pinned_lexical'}
        now, eligible = datetime.now(UTC), []
        for memory in candidates.values():
            if (((memory.get('metadata') or {}).get('avatar_id') or '') != owner or
                    safety.blocks_message(memory.get('content') or '') or not can_recall(memory, ds)):
                continue
            ranking = describe(memory.get('metadata'), now, memory.get('similarity'))
            eligible.append({**memory, 'ranking': ranking})
        return sorted(eligible, key=lambda m: (-(m['ranking']['ranking_score'] or 0), str(m['id'])))[:limit]

    @staticmethod
    def capture_recall(ds, owner, memories):
        """Remember selected input identities, not a second copy of personal text."""
        if not memories:
            return {}
        from core.knowledge import floor
        return {'avatar_id': owner or '', 'dataset_epoch': (ds.get_key('dataset_epoch') or ds.dataset_epoch())['value'],
                'recall_after': floor(ds, owner),
                'items': [{'id': m['id'], 'sha256': hashlib.sha256(m['content'].encode('utf-8')).hexdigest()}
                          for m in memories[:20]]}

    def record_recalled(self, ds, captured, user_id, assistant_id, now=None):
        """Advance recency only after one eligible, saved completed exchange."""
        if not captured:
            return 0
        from core.knowledge import can_recall, dirty, floor
        from core.salience import describe
        from core.turns import get_turn_service
        with get_turn_service(ds).lock:
            owner = captured.get('avatar_id') or ''
            if (dirty(ds, owner) or (ds.get_key('dataset_epoch') or {}).get('value') != captured.get('dataset_epoch') or
                    floor(ds, owner) != captured.get('recall_after')):
                return 0
            user, assistant = ds.get_message(user_id), ds.get_message(assistant_id)
            session = ds.get_session(user.session_id) if user else None
            if (not session or (session.avatar_id or '') != owner or not assistant or assistant.session_id != user.session_id or
                    user.role != 'user' or assistant.role != 'assistant' or user_id >= assistant_id):
                return 0
            now = now or datetime.now(UTC)
            if now.tzinfo is None:
                raise ValueError('Recall clock must include its offset.')
            updated = 0
            for item in captured.get('items', [])[:20]:
                raw = self.store.collection.get(ids=[item['id']])
                if not raw.get('ids'):
                    continue
                metadata = raw['metadatas'][0] or {}
                memory = {'id': item['id'], 'content': raw['documents'][0] or '', 'metadata': metadata}
                prior = metadata.get('last_recall_message_id') or 0
                if (type(prior) is not int or prior >= assistant_id or (metadata.get('avatar_id') or '') != owner or
                        hashlib.sha256(memory['content'].encode('utf-8')).hexdigest() != item['sha256'] or not can_recall(memory, ds)):
                    continue
                self.store.collection.update(ids=[item['id']], metadatas=[{**metadata,
                    'last_recalled_at': now.astimezone(UTC).isoformat(), 'last_recall_message_id': assistant_id,
                    'recall_count': describe(metadata, now)['recall_count'] + 1}])
                updated += 1
            return updated

    def format_memories_for_prompt(self, memories):
        """Format memories for injection into system prompt."""
        if not memories:
            return ""

        lines = ["\nRelevant memories about the user:\n"]
        for mem in memories:
            content = mem["content"]
            meta = mem.get("metadata", {})
            confidence = float(meta.get("confidence", 0))
            mem_type = meta.get("type", "fact")

            # Only include high-confidence memories
            if confidence < 0.6:
                continue

            type_icon = {
                "fact": "📌",
                "preference": "💡",
                "event": "📅"
            }.get(mem_type, "•")

            lines.append(f"{type_icon} [{mem_type}] {content}")

        return "\n".join(lines) + "\n"

    def get_all_memories(self, avatar_id=None):
        """Get all memories for display (or only one companion's)."""
        return self.store.get_all(avatar_id=avatar_id)

    def update_memory_confidence(self, memory_id, new_confidence):
        """Update confidence of a specific memory."""
        self.store.update_memory(memory_id, confidence=new_confidence)

    def delete_memory(self, memory_id):
        """Delete a specific memory."""
        self.store.delete_memory(memory_id)

    def clear_all_memories(self):
        """Clear all memories (with warning)."""
        self.store.clear_all()


# ---- memories written by the chat model -----------------------------------------------------------------------------
#
# The phrase matcher below is the fallback. The normal path is a short batch of the person's own messages, sent to the
# chat model after a reply, which writes statements that stand on their own. Batches keep this off the reply's critical
# path and off the GPU for every single message.

MEMORY_TYPES = ("fact", "preference", "event")
DEFAULT_MEMORY_SETTINGS = {
    "every_messages": 4,      # run once this many of the person's messages have not been read yet
    "batch_messages": 30,     # at most this many messages in one model call
    "max_memories": 8,        # at most this many memories from one batch
}
MAX_BATCH_CHARS = 12000       # about 3.5k tokens of conversation per call
EXTRACT_MAX_TOKENS = 3000     # room for a reasoning model to think before its JSON
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.I)
_SPEAKER = re.compile(r"^(?:them|user|person|human)\s*:\s*", re.I)
_BULLET = re.compile(r"^[-*•]\s*")


def settings(config=None) -> dict:
    cfg = dict(DEFAULT_MEMORY_SETTINGS)
    user = (((config or {}).get("companion") or {}).get("memory") or {})
    for key in DEFAULT_MEMORY_SETTINGS:
        if key in user:
            try:
                cfg[key] = max(1, int(user[key]))
            except (TypeError, ValueError):
                pass
    cfg["max_memories"] = min(20, cfg["max_memories"])
    return cfg


def build_extraction_request(name: str, conversation: str, today: str, max_memories: int = 8) -> list:
    """The prompt for the model that writes memories. The rules match the notebook's: user words only, no guesses."""
    system = (f"You keep the private memory list of {name}, an AI companion, about the person they talk to. You turn what "
              "the person said into short statements worth keeping. You answer with a JSON array and nothing else. "
              "Use only the messages below as evidence. "
              "Never promote companion guesses, hypotheticals or roleplay into facts about the real person.")
    user = f"""Here is what the person said to {name}, oldest first. Each line starts with the number of the message it came from.

{conversation}

Write the memories worth keeping. Today is {today}.
- A memory is one short statement that stands on its own without the chat, for example "Is afraid of the sea since a storm on 2026-11-10". Do not copy a message word for word; say what it means for the person.
- Use type "fact" for who they are and what is true of them, "preference" for what they like or dislike, and "event" for something that happened or is planned.
- Turn relative dates into absolute ones using today's date.
- Only what the person said or clearly showed about themselves. Never guess, never invent. Nothing {name} said, nothing imagined, roleplay or a requested picture is a real event.
- If they corrected something, keep the newest version.
- Confidence means how sure the statement is: 0.9-0.95 when they said it plainly, 0.6-0.8 when it is implied or uncertain. Do not use one number for everything.
- At most {max_memories} memories. If nothing is worth keeping, answer [].
- "from" lists the message numbers the memory came from.

Answer with a JSON array and nothing else:
[{{"content": "...", "type": "fact", "confidence": 0.9, "from": [12]}}]"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _plain(text: str) -> str:
    """A comparison form for a quoted message: case, punctuation and spacing do not matter."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def parse_extracted(text: str, conversation: str = "", max_memories: int = 8):
    """The model's JSON array as memory dicts, or None when the answer cannot be read (so the fallback can run).

    An empty array is a real answer: the model read the batch and found nothing worth keeping.
    """
    text = _THINK.sub("", text or "").strip()
    text = _FENCE.sub("", text).strip()
    data = None
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            continue
        break
    if isinstance(data, dict):
        data = data.get("memories")
    if not isinstance(data, list):
        return None

    quoted = {_plain(re.sub(r"^\[\d+\]\s*", "", line)) for line in (conversation or "").splitlines()}
    found, seen = [], set()
    for row in data:
        if isinstance(row, str):
            row = {"content": row}
        if not isinstance(row, dict):
            continue
        content = _BULLET.sub("", str(row.get("content") or row.get("text") or row.get("memory") or "").strip())
        content = _SPEAKER.sub("", content).strip()
        while len(content) > 2 and content[0] in "\"“'‘" and content[-1] in "\"”'’":
            content = content[1:-1].strip()
        content = " ".join(content.split()).rstrip(".")
        if not 3 <= len(content) <= 400:
            continue
        # A whole message copied back is not a memory: it is the old phrase matcher's failure mode.
        if _plain(content) in quoted:
            continue
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)
        kind = str(row.get("type") or "fact").strip().lower()
        try:
            confidence = float(row.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        sources = row.get("from") or row.get("source_ids") or []
        sources = [value for value in sources if type(value) is int] if isinstance(sources, list) else []
        found.append({"content": content, "type": kind if kind in MEMORY_TYPES else "fact",
                      "confidence": min(0.99, max(0.3, confidence)), "from": sources})
        if len(found) >= max_memories:
            break
    return found


def _scan_key(owner) -> str:
    return "memory_scan:" + (owner or "@default")


class MemoryWriter:
    """Reads the person's own messages in batches and has the chat model write memories from them.

    It runs after a reply only when enough unread messages have gathered, so a reply never waits for it. The notes
    service's per-companion reservation is shared, so notes and memories never ask the model at the same time for the
    same person. When the model is unreachable or its answer cannot be read, the phrase matcher writes the memories
    instead and they are marked `extracted_by='phrases'`.

    llm: a function(messages) -> text. Tests pass a fake; by default the chat model is used.
    sync: run the job inline (tests).
    """

    def __init__(self, manager_getter, datastore_getter, config_getter=None, llm=None, notes=None, sync=False):
        self._manager, self._ds = manager_getter, datastore_getter
        self._config, self._llm, self._notes, self.sync = config_getter, llm, notes, sync
        self._own_busy = set()
        self._lock = threading.Lock()

    @property
    def notes(self):
        if self._notes is not None:
            return self._notes
        from core.notes import get_notes_service
        return get_notes_service()

    def cfg(self) -> dict:
        return settings(self._config() if self._config else {})

    # ---- where the reading stopped ---------------------------------------------------------------------------------
    def through(self, owner) -> int:
        """Where reading stopped. A database that had chats before the writer existed starts after them (see
        datastore._start_memory_reading_after_existing_history)."""
        return int((self._ds().get_key(_scan_key(owner)) or {}).get("through") or 0)

    def mark_through(self, owner, message_id) -> None:
        self._ds().set_key(_scan_key(owner), {"through": int(message_id or 0)})

    # ---- which messages are still unread ---------------------------------------------------------------------------
    def candidates(self, owner) -> list:
        """The person's own messages after the last read point, oldest first, that may become memories."""
        from core import safety
        from core.knowledge import floor, prompt_messages, sessions
        ds = self._ds()
        after = max(self.through(owner), floor(ds, owner))
        found = []
        for sid in sessions(ds, owner):
            for message in prompt_messages(ds, sid, ds.get_messages_after(sid, after), allow_derived=False):
                if message.role == "user" and message.id > after and not safety.blocks_message(message.content):
                    found.append(message)
        found.sort(key=lambda message: message.id)
        return self._not_turned_away(owner, found)

    @staticmethod
    def _not_turned_away(owner, messages) -> list:
        """Kids mode answers some messages with a fixed reply and never shows them to the model; those are not memories."""
        if not owner:
            return messages
        try:
            from core import kids
            from core import companion
            record = companion.get_services().avatars.get(owner)
        except Exception:
            # Fail closed: without the avatar record we cannot tell a child's turned-away message from an adult's.
            LOGGER.exception("Could not check kids mode before writing memories; skipping this pass")
            return []
        if not record or not kids.is_on(record):
            return messages
        return [message for message in messages if kids.reply_for_message(message.content) is None]

    def due(self, owner) -> bool:
        from core import companions
        from core.knowledge import dirty
        if not companions.feature_on(owner, "memory") or dirty(self._ds(), owner):
            return False
        return len(self.candidates(owner)) >= self.cfg()["every_messages"]

    # ---- running ---------------------------------------------------------------------------------------------------
    def schedule(self, owner):
        """Start a memory pass in the background when it is due. Never raises, never blocks (unless sync)."""
        try:
            if not self.due(owner) or not self._reserve(owner):
                return None
        except Exception:
            LOGGER.exception("Could not plan a memory pass")
            return None
        if self.sync or getattr(self.notes, "sync", False):
            try:
                self.run(owner)
            finally:
                self._release(owner)
            return "memory"
        try:
            from core.state_gate import start_job
            start_job(self._job, (owner,), name="altera-memories")
        except Exception:
            self._release(owner)
            LOGGER.exception("Could not start a memory pass")
            return None
        return "memory"

    def _job(self, owner):
        try:
            return self.run(owner)
        except Exception:
            LOGGER.exception("Memory pass failed")
            return {"ok": False, "stored": 0}
        finally:
            self._release(owner)

    def _reserve(self, owner) -> bool:
        notes = self.notes
        if notes is not None:
            return notes.reserve(owner)
        with self._lock:
            if owner in self._own_busy:
                return False
            self._own_busy.add(owner)
            return True

    def _release(self, owner) -> None:
        notes = self.notes
        if notes is not None:
            notes.release(owner)
            return
        with self._lock:
            self._own_busy.discard(owner)

    def run(self, owner, force=False) -> dict:
        """Read every unread message for this person and save what is worth keeping. The caller holds the reservation."""
        from core import companions
        from core.knowledge import dirty
        ds = self._ds()
        if not force:
            if not companions.feature_on(owner, "memory"):
                return {"ok": True, "stored": 0, "skipped": "memory is off"}
            if dirty(ds, owner):
                return {"ok": False, "stored": 0, "error": "Repair memories before writing new ones."}
        pending = self.candidates(owner)
        if not pending:
            return {"ok": True, "stored": 0, "scanned": 0}
        stored, fallback = 0, False
        for group in self._groups(pending):
            if not force and dirty(ds, owner):
                return {"ok": False, "stored": stored, "error": "The source conversation changed while writing memories."}
            added, used = self._extract_group(owner, group)
            stored += added
            fallback = fallback or used == "phrases"
            self.mark_through(owner, group[-1].id)
        return {"ok": True, "stored": stored, "scanned": len(pending), "fallback": fallback}

    def rebuild(self, owner) -> dict:
        """Start again from the recall boundary: the caller has already removed the stale automatic memories."""
        from core.knowledge import floor
        self.mark_through(owner, floor(self._ds(), owner))
        return self.run(owner, force=True)

    def _groups(self, messages) -> list:
        cfg, groups, group, size = self.cfg(), [], [], 0
        for message in messages:
            cost = len(message.content or "") + 16
            if group and (len(group) >= cfg["batch_messages"] or size + cost > MAX_BATCH_CHARS):
                groups.append(group)
                group, size = [], 0
            group.append(message)
            size += cost
        if group:
            groups.append(group)
        return groups

    def _extract_group(self, owner, group):
        from core.knowledge import reference
        refs = {message.id: reference(message) for message in group}
        conversation = "\n".join(f"[{message.id}] {(message.content or '').strip()}" for message in group)
        found, used = None, "model"
        try:
            request = build_extraction_request(self._name(owner), conversation, datetime.now().date().isoformat(),
                                               self.cfg()["max_memories"])
            answer = self._call(request)
            if not str(answer or "").strip():
                answer = self._call(request)          # a reasoning model's empty first answer, asked once more
            found = parse_extracted(answer, conversation, self.cfg()["max_memories"])
        except Exception as error:
            LOGGER.warning("The chat model could not write memories, so the phrase matcher will: %s", error)
        if found is None:
            used = "phrases"
            found = [{**memory, "refs": [refs[message.id]]}
                     for message in group for memory in self._manager().extractor.extract_from_text(message.content)]
        else:
            for memory in found:
                wanted = [refs[number] for number in memory.get("from", []) if number in refs]
                memory["refs"] = wanted or list(refs.values())
        return len(self._manager().store_extracted(found, avatar_id=owner, extracted_by=used)), used

    def _name(self, owner) -> str:
        try:
            from core import companion
            record = companion.get_services().avatars.get(owner) if owner else None
            name = (record or {}).get("name")
        except Exception:
            name = None
        return name or "the companion"

    def _call(self, messages) -> str:
        if self._llm:
            return self._llm(messages)
        from core.llm import LLMClient
        base = LLMClient.get_instance(self._config() if self._config else None)
        client = LLMClient(base_url=base.base_url, api_key=base.api_key, model=base.model,
                           temperature=0.2, max_tokens=EXTRACT_MAX_TOKENS)
        return client.chat(messages, stream=False)


# Singleton instance
memory_manager = MemoryManager()


def get_memory_manager():
    """Return the singleton memory manager instance."""
    return memory_manager


_writer = None
_writer_owner = None
_writer_pinned = False
_writer_lock = threading.Lock()


def get_memory_writer() -> MemoryWriter:
    """The shared writer, rebuilt when the app's services (and so its config and avatar store) change."""
    global _writer, _writer_owner
    with _writer_lock:
        if _writer_pinned and _writer is not None:
            return _writer
        from core import companion
        from core.datastore import get_datastore
        services = companion.get_services()
        if _writer is None or _writer_owner is not services:
            _writer = MemoryWriter(get_memory_manager, get_datastore, lambda: services.config)
            _writer_owner = services
        return _writer


def set_memory_writer(writer) -> None:
    """Pin the shared writer (or, with None, forget it and build a fresh one on demand). Used by tests."""
    global _writer, _writer_owner, _writer_pinned
    with _writer_lock:
        _writer, _writer_owner, _writer_pinned = writer, None, writer is not None

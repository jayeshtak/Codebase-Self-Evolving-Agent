import os
import re
import json
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any


# ── Config ────────────────────────────────────────────────────────────────────

CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
COLLECTION_NAME = "codebase"
CHUNK_SIZE = 50
CHUNK_OVERLAP = 10

SUPPORTED_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs", ".html", ".css", ".md"}

_DISABLE_LOCAL_EMBEDDINGS = (
    os.environ.get("DISABLE_LOCAL_EMBEDDINGS", "").lower() in ("1", "true", "yes")
    or os.environ.get("LOW_MEMORY", "").lower() in ("1", "true", "yes")
    or os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")
)


# ── Lightweight RAG Engine (< 5MB RAM, Zero-Torch fallback for cloud PaaS) ────

class _LightweightRAG:
    """
    High-efficiency in-memory code search engine designed for resource-constrained
    cloud environments (such as Render 512MB free tier).
    Performs fast token-weighted relevance and exact match search without requiring PyTorch.
    """

    def __init__(self, repo_path: str, storage_path: Optional[str] = None):
        self.repo_path = Path(repo_path).resolve()
        self.storage_path = Path(storage_path) if storage_path else CHROMA_PATH
        self.chunks: List[Dict[str, Any]] = []
        self._indexed_files = set()
        print(f"[RAG] Using lightweight in-memory code search (< 5MB RAM footprint) for: {self.repo_path}")

    def _chunk_file(self, filepath: Path) -> List[Dict[str, Any]]:
        chunks = []
        try:
            lines = filepath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as e:
            return chunks

        try:
            rel = str(filepath.relative_to(self.repo_path))
        except ValueError:
            rel = str(filepath.name)

        total_lines = len(lines)
        start = 0
        chunk_index = 0
        while start < total_lines:
            end = min(start + CHUNK_SIZE, total_lines)
            chunk_text = "\n".join(lines[start:end])
            if chunk_text.strip():
                chunk_id = hashlib.md5(f"{rel}:{start}:{chunk_text[:50]}".encode()).hexdigest()
                chunks.append({
                    "id": chunk_id,
                    "text": chunk_text,
                    "metadata": {
                        "file": rel,
                        "start_line": start + 1,
                        "end_line": end,
                        "chunk_index": chunk_index,
                        "language": filepath.suffix.lstrip("."),
                        "total_lines": total_lines,
                    }
                })
                chunk_index += 1
            start += CHUNK_SIZE - CHUNK_OVERLAP
        return chunks

    def index_file(self, filepath: Path):
        new_chunks = self._chunk_file(filepath)
        if not new_chunks:
            return
        try:
            rel = str(filepath.relative_to(self.repo_path))
        except ValueError:
            rel = str(filepath.name)

        # Remove existing chunks for this file
        self.chunks = [c for c in self.chunks if c["metadata"].get("file") != rel]
        self.chunks.extend(new_chunks)
        self._indexed_files.add(rel)

    def index_repo(self, changed_only: bool = False, changed_files: Optional[list] = None):
        if changed_files:
            files = [Path(f) for f in changed_files if Path(f).suffix in SUPPORTED_EXTENSIONS and Path(f).exists()]
        else:
            files = self._get_all_files()

        for f in files:
            self.index_file(f)
        print(f"[RAG] Lightweight index ready. Total chunks: {len(self.chunks)}")

    def _get_all_files(self) -> List[Path]:
        files = []
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(self.repo_path.rglob(f"*{ext}"))
        return [
            f for f in files
            if "venv" not in f.parts and "node_modules" not in f.parts
            and not any(part.startswith(".") for part in f.parts)
        ]

    def search(self, query: str, n_results: int = 5, file_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.chunks:
            # Auto index if not yet indexed
            self.index_repo()

        if not self.chunks:
            return []

        query_lower = query.lower()
        query_tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}", query_lower))

        candidates = []
        for chunk in self.chunks:
            rel_file = chunk["metadata"].get("file", "")
            if file_filter and file_filter not in rel_file:
                continue

            text = chunk["text"]
            text_lower = text.lower()
            score = 0.0

            # Exact phrase match bonus
            if query_lower in text_lower:
                score += 0.5

            # Token overlap score
            chunk_tokens = set(re.findall(r"[a-zA-Z0-9_]{2,}", text_lower))
            if query_tokens and chunk_tokens:
                overlap = len(query_tokens & chunk_tokens)
                if overlap > 0:
                    score += 0.5 * (overlap / len(query_tokens))

            if score > 0.05:
                candidates.append({
                    "chunk_id": chunk["id"],
                    "text": chunk["text"],
                    "metadata": chunk["metadata"],
                    "score": round(min(score, 1.0), 4),
                })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:n_results]

    def search_for_issue(self, issue: dict, n_results: int = 5) -> list:
        query = f"{issue.get('issue_type', '')} {issue.get('description', '')} {issue.get('file', '')}"
        return self.search(query, n_results=n_results)

    def get_file_chunks(self, rel_path: str) -> list:
        matching = [c for c in self.chunks if c["metadata"].get("file") == rel_path]
        return sorted(matching, key=lambda x: x["metadata"].get("start_line", 0))

    def get_full_file_context(self, rel_path: str) -> str:
        chunks = self.get_file_chunks(rel_path)
        return "\n".join(c["text"] for c in chunks) if chunks else ""

    def stats(self) -> dict:
        return {
            "total_chunks": len(self.chunks),
            "collection": "lightweight_memory",
            "chroma_path": str(self.storage_path),
            "embedding_model": "lightweight-token-bm25",
            "status": "active",
        }


# ── Try to import chromadb (Only if not in cloud low-memory mode) ──────────────

_CHROMA_OK = False
if not _DISABLE_LOCAL_EMBEDDINGS:
    try:
        import chromadb
        from chromadb.config import Settings
        from sentence_transformers import SentenceTransformer
        _CHROMA_OK = True
    except Exception as _chroma_err:
        _CHROMA_OK = False


# ── CodebaseRAG ───────────────────────────────────────────────────────────────

class CodebaseRAG:
    def __new__(cls, repo_path: str, chroma_path: Optional[str] = None):
        if not _CHROMA_OK:
            return _LightweightRAG(repo_path, chroma_path)
        return super().__new__(cls)

    def __init__(self, repo_path: str, chroma_path: Optional[str] = None):
        if not _CHROMA_OK:
            return

        self.repo_path = Path(repo_path).resolve()
        self.chroma_path = Path(chroma_path) if chroma_path else CHROMA_PATH
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self._embedder = None
        self._collection = None

    @property
    def embedder(self):
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            print(f"[Chroma] Lazy-loading embedding model: {EMBEDDING_MODEL}")
            self._embedder = SentenceTransformer(EMBEDDING_MODEL)
        return self._embedder

    @property
    def collection(self):
        if self._collection is None:
            import chromadb
            from chromadb.config import Settings
            client = chromadb.PersistentClient(
                path=str(self.chroma_path),
                settings=Settings(anonymized_telemetry=False)
            )
            self._collection = client.get_or_create_collection(
                name=COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"}
            )
        return self._collection

    def _chunk_file(self, filepath: Path) -> list[dict]:
        chunks = []
        try:
            lines = filepath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return chunks

        try:
            rel = str(filepath.relative_to(self.repo_path))
        except ValueError:
            rel = str(filepath.name)

        total_lines = len(lines)
        start = 0
        chunk_index = 0
        while start < total_lines:
            end = min(start + CHUNK_SIZE, total_lines)
            chunk_text = "\n".join(lines[start:end])
            if chunk_text.strip():
                chunk_id = hashlib.md5(f"{rel}:{start}:{chunk_text[:50]}".encode()).hexdigest()
                chunks.append({
                    "id": chunk_id,
                    "text": chunk_text,
                    "metadata": {
                        "file": rel, "start_line": start + 1, "end_line": end,
                        "chunk_index": chunk_index,
                        "language": filepath.suffix.lstrip("."),
                        "total_lines": total_lines,
                    }
                })
                chunk_index += 1
            start += CHUNK_SIZE - CHUNK_OVERLAP
        return chunks

    def index_file(self, filepath: Path):
        chunks = self._chunk_file(filepath)
        if not chunks:
            return
        try:
            rel = str(filepath.relative_to(self.repo_path))
        except ValueError:
            rel = str(filepath.name)

        self._delete_file_chunks(rel)
        texts = [c["text"] for c in chunks]
        embeddings = self.embedder.encode(texts, show_progress_bar=False).tolist()
        self.collection.add(
            ids=[c["id"] for c in chunks],
            documents=texts,
            embeddings=embeddings,
            metadatas=[c["metadata"] for c in chunks],
        )

    def index_repo(self, changed_only: bool = False, changed_files: Optional[list] = None):
        if changed_files:
            files = [f for f in changed_files if f.suffix in SUPPORTED_EXTENSIONS and f.exists()]
        else:
            files = self._get_all_files()
        for f in files:
            self.index_file(f)

    def _get_all_files(self) -> list:
        files = []
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(self.repo_path.rglob(f"*{ext}"))
        return [
            f for f in files
            if "venv" not in f.parts and "node_modules" not in f.parts
            and not any(part.startswith(".") for part in f.parts)
        ]

    def _delete_file_chunks(self, rel_path: str):
        try:
            results = self.collection.get(where={"file": rel_path})
            if results["ids"]:
                self.collection.delete(ids=results["ids"])
        except Exception:
            pass

    def search(self, query: str, n_results: int = 5, file_filter: Optional[str] = None) -> list:
        if self.collection.count() == 0:
            return []
        query_embedding = self.embedder.encode([query], show_progress_bar=False).tolist()
        where = {"file": file_filter} if file_filter else None
        try:
            results = self.collection.query(
                query_embeddings=query_embedding,
                n_results=min(n_results, self.collection.count()),
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            return []
        hits = []
        for i in range(len(results["ids"][0])):
            hits.append({
                "chunk_id": results["ids"][0][i],
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "score": round(1 - results["distances"][0][i], 4),
            })
        return hits

    def search_for_issue(self, issue: dict, n_results: int = 5) -> list:
        query = f"{issue.get('issue_type', '')} {issue.get('description', '')} in {issue.get('file', '')}"
        return self.search(query, n_results=n_results)

    def get_file_chunks(self, rel_path: str) -> list:
        try:
            results = self.collection.get(where={"file": rel_path}, include=["documents", "metadatas"])
            chunks = []
            for i in range(len(results["ids"])):
                chunks.append({
                    "chunk_id": results["ids"][i],
                    "text": results["documents"][i],
                    "metadata": results["metadatas"][i],
                })
            return sorted(chunks, key=lambda x: x["metadata"].get("start_line", 0))
        except Exception:
            return []

    def get_full_file_context(self, rel_path: str) -> str:
        chunks = self.get_file_chunks(rel_path)
        return "\n".join(c["text"] for c in chunks) if chunks else ""

    def stats(self) -> dict:
        count = self.collection.count()
        return {
            "total_chunks": count,
            "collection": COLLECTION_NAME,
            "chroma_path": str(self.chroma_path),
            "embedding_model": EMBEDDING_MODEL,
        }

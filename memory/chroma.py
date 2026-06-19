import os
import json
import hashlib
from pathlib import Path
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────

CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma"
EMBEDDING_MODEL = "all-mpnet-base-v2"
COLLECTION_NAME = "codebase"
CHUNK_SIZE = 50
CHUNK_OVERLAP = 10

SUPPORTED_EXTENSIONS = {".py", ".js", ".ts", ".go", ".rs"}


# ── Null fallback (when chromadb is unavailable) ──────────────────────────────

class _NullRAG:
    """Drop-in replacement used when chromadb can't be imported."""

    def __init__(self, *a, **kw):
        print("[Chroma] chromadb unavailable (version conflict). RAG disabled — continuing without it.")

    def index_file(self, filepath): pass
    def index_repo(self, **kw): pass
    def search(self, query: str, n_results: int = 5, **kw) -> list: return []
    def search_for_issue(self, issue: dict, n_results: int = 5) -> list: return []
    def get_file_chunks(self, rel_path: str) -> list: return []
    def get_full_file_context(self, rel_path: str) -> str: return ""
    def stats(self) -> dict: return {"total_chunks": 0, "status": "disabled"}


# ── Try to import chromadb ────────────────────────────────────────────────────

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
            return _NullRAG(repo_path, chroma_path)
        return super().__new__(cls)

    def __init__(self, repo_path: str, chroma_path: Optional[str] = None):
        if not _CHROMA_OK:
            return  # already handled by _NullRAG

        self.repo_path = Path(repo_path).resolve()
        self.chroma_path = Path(chroma_path) if chroma_path else CHROMA_PATH
        self.chroma_path.mkdir(parents=True, exist_ok=True)

        print(f"[Chroma] Loading embedding model: {EMBEDDING_MODEL}")
        self.embedder = SentenceTransformer(EMBEDDING_MODEL)

        self.client = chromadb.PersistentClient(
            path=str(self.chroma_path),
            settings=Settings(anonymized_telemetry=False)
        )
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}
        )
        print(f"[Chroma] Collection '{COLLECTION_NAME}' ready. "
              f"Existing chunks: {self.collection.count()}")

    # ── Chunking ──────────────────────────────────────────────────────────────

    def _chunk_file(self, filepath: Path) -> list[dict]:
        chunks = []
        try:
            lines = filepath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as e:
            print(f"[Chroma] Read error on {filepath}: {e}")
            return chunks

        rel = str(filepath.relative_to(self.repo_path))
        total_lines = len(lines)
        start = 0
        chunk_index = 0
        while start < total_lines:
            end = min(start + CHUNK_SIZE, total_lines)
            chunk_text = "\n".join(lines[start:end])
            if chunk_text.strip():
                chunk_id = hashlib.md5(
                    f"{rel}:{start}:{chunk_text}".encode()
                ).hexdigest()
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

    # ── Indexing ──────────────────────────────────────────────────────────────

    def index_file(self, filepath: Path):
        chunks = self._chunk_file(filepath)
        if not chunks:
            return
        rel = str(filepath.relative_to(self.repo_path))
        self._delete_file_chunks(rel)
        texts = [c["text"] for c in chunks]
        embeddings = self.embedder.encode(texts, show_progress_bar=False).tolist()
        self.collection.add(
            ids=[c["id"] for c in chunks],
            documents=texts,
            embeddings=embeddings,
            metadatas=[c["metadata"] for c in chunks],
        )
        print(f"[Chroma] Indexed {filepath.relative_to(self.repo_path)} ({len(chunks)} chunk(s))")

    def index_repo(self, changed_only: bool = False, changed_files: Optional[list] = None):
        if changed_files:
            files = [f for f in changed_files if f.suffix in SUPPORTED_EXTENSIONS and f.exists()]
        elif changed_only:
            files = self._get_all_files()
        else:
            files = self._get_all_files()
        print(f"[Chroma] Indexing {len(files)} file(s)...")
        for f in files:
            self.index_file(f)
        print(f"[Chroma] Done. Total chunks: {self.collection.count()}")

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
        except Exception as e:
            print(f"[Chroma] Delete error for {rel_path}: {e}")

    # ── Search ────────────────────────────────────────────────────────────────

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
        except Exception as e:
            print(f"[Chroma] Search error: {e}")
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
        except Exception as e:
            print(f"[Chroma] Get file chunks error: {e}")
            return []

    def get_full_file_context(self, rel_path: str) -> str:
        chunks = self.get_file_chunks(rel_path)
        return "\n".join(c["text"] for c in chunks) if chunks else ""

    def stats(self) -> dict:
        count = self.collection.count()
        return {"total_chunks": count, "collection": COLLECTION_NAME,
                "chroma_path": str(self.chroma_path), "embedding_model": EMBEDDING_MODEL}


# ── CLI test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    rag = CodebaseRAG(repo)
    print(f"[Chroma] Stats: {rag.stats()}")

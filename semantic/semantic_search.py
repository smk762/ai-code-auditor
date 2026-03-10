from __future__ import annotations

from semantic.embedding_index import EmbeddingIndex


class SemanticSearch:
    def __init__(self, index: EmbeddingIndex):
        self.index = index

    def query(self, text: str, top_k: int = 5) -> list[dict]:
        return self.index.search(query=text, top_k=top_k)

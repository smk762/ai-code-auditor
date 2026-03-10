from __future__ import annotations

from pathlib import Path
import yaml

from auditor.contracts import CopilotResponse
from copilot.context_orchestrator import ContextOptions, ContextOrchestrator
from copilot.query_router import classify_query
from ecosystem.graph_builder import GraphBuilder
from semantic.semantic_search import SemanticSearch


class CopilotEngine:
    def __init__(self, search: SemanticSearch, graph: GraphBuilder):
        self.search = search
        self.graph = graph

    def ask(
        self,
        query: str,
        *,
        context_profile: str = "balanced",
        max_context_tokens: int = 2200,
        enable_compaction: bool = True,
        enable_notes: bool = True,
        enable_subagents: bool = False,
        external_agent_brief: bool = False,
        notes_path: str = "memory/context_notes.md",
    ) -> CopilotResponse:
        lower_query = query.lower()
        if any(
            phrase in lower_query
            for phrase in (
                "extraction candidate",
                "shared package",
                "duplicated logic cluster",
                "shared-auth",
                "retry logic",
            )
        ):
            return self._answer_extraction_query(query)

        mode = classify_query(query)
        semantic_hits = self.search.query(query, top_k=5)
        orchestrator = ContextOrchestrator(
            ContextOptions(
                profile=context_profile,
                max_context_tokens=max_context_tokens,
                enable_compaction=enable_compaction,
                enable_notes=enable_notes,
                enable_subagents=enable_subagents,
                notes_path=notes_path,
            )
        )
        context_payload = orchestrator.orchestrate(query=query, semantic_hits=semantic_hits)
        repos = sorted({hit.get("repo", "") for hit in semantic_hits if hit.get("repo")})
        supporting = [f"{hit.get('repo')}::{hit.get('symbol')}" for hit in semantic_hits]
        supporting.extend(
            [f"context::{chunk.get('citation', '')}" for chunk in context_payload.get("chunks", []) if chunk.get("citation")]
        )

        answer = self._build_answer(mode, query, repos, supporting)
        if context_payload.get("summary"):
            answer = f"{answer}\n\nContext summary:\n{context_payload['summary']}"
        if external_agent_brief:
            brief = orchestrator.build_external_agent_brief(query=query, payload=context_payload, impacted_repos=repos)
            answer = f"{answer}\n\n{brief}"
        confidence = 0.8 if semantic_hits else 0.35
        citations = [f"{hit.get('file_path')}::{hit.get('symbol')}" for hit in semantic_hits]
        citations.extend(
            [chunk.get("citation", "") for chunk in context_payload.get("chunks", []) if chunk.get("citation")]
        )
        deduped_citations: list[str] = []
        for cite in citations:
            if cite and cite not in deduped_citations:
                deduped_citations.append(cite)
        return CopilotResponse(
            answer=answer,
            supporting_entities=supporting,
            impacted_repos=repos,
            confidence=confidence,
            citations=deduped_citations,
        )

    def deep_research(self, query: str, max_iterations: int = 3) -> dict:
        iterations: list[dict] = []
        all_citations: set[str] = set()
        all_repos: set[str] = set()

        current_query = query
        for i in range(1, max_iterations + 1):
            hits = self.search.query(current_query, top_k=8)
            citations = [f"{hit.get('file_path')}::{hit.get('symbol')}" for hit in hits]
            repos = sorted({hit.get("repo", "") for hit in hits if hit.get("repo")})
            all_citations.update(citations)
            all_repos.update(repos)
            insight = self._build_answer(
                mode=classify_query(current_query),
                query=current_query,
                repos=repos,
                supporting=[f"{hit.get('repo')}::{hit.get('symbol')}" for hit in hits],
            )
            iterations.append(
                {
                    "iteration": i,
                    "query": current_query,
                    "insight": insight,
                    "repos": repos,
                    "citations": citations[:5],
                }
            )
            current_query = f"{query} architecture impact dependencies iteration {i}"

        conclusion = (
            f"Deep research suggests impact across repos: {', '.join(sorted(all_repos))}."
            if all_repos
            else "Deep research found limited evidence. Rebuild index and expand repository coverage."
        )
        return {
            "plan": f"Investigate query '{query}' via semantic retrieval and cross-repo evidence synthesis.",
            "updates": iterations,
            "conclusion": conclusion,
            "citations": sorted(all_citations),
            "confidence": 0.85 if all_citations else 0.4,
        }

    def _build_answer(self, mode: str, query: str, repos: list[str], supporting: list[str]) -> str:
        if mode == "dependency" and repos:
            return f"Likely dependencies for query '{query}' involve repos: {', '.join(repos)}."
        if mode == "impact" and repos:
            return f"Potential blast radius touches repos: {', '.join(repos)}."
        if mode == "ownership" and supporting:
            return f"Most relevant code units: {', '.join(supporting[:3])}."
        if repos:
            return f"Relevant ecosystem context found in repos: {', '.join(repos)}."
        return "No strong matches found yet. Rebuild the semantic index or narrow the question."

    def _answer_extraction_query(self, query: str) -> CopilotResponse:
        path = Path("reports/extraction_candidates.yaml")
        if not path.exists():
            return CopilotResponse(
                answer="No extraction candidate report found. Run ecosystem audit first.",
                supporting_entities=[],
                impacted_repos=[],
                confidence=0.3,
                citations=["reports/extraction_candidates.yaml"],
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        candidates = raw.get("candidates", [])
        if not candidates:
            return CopilotResponse(
                answer="No extraction candidates currently exceed proposal threshold.",
                supporting_entities=[],
                impacted_repos=[],
                confidence=0.5,
                citations=["reports/extraction_candidates.yaml"],
            )
        top = candidates[0]
        repos = top.get("repos_involved", [])
        title = top.get("title", "Top extraction candidate")
        answer = f"{title} is currently top-ranked. Impacted repos: {', '.join(repos)}."
        return CopilotResponse(
            answer=answer,
            supporting_entities=[title],
            impacted_repos=repos,
            confidence=0.8,
            citations=["reports/extraction_candidates.yaml", "reports/extraction_candidates.md"],
        )

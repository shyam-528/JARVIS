from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ResearchSource:
    title: str
    url: str
    snippet: str
    origin: str  # "web" | "knowledge"


@dataclass
class ResearchReport:
    topic: str
    summary: str
    sources: list[ResearchSource] = field(default_factory=list)
    report_path: str = ""
    provider: str = ""


class DeepResearcher:
    """Research Agent 2.0: gather from multiple sources, synthesize with
    numbered citations, and save a durable markdown report.

    Sources: DuckDuckGo web search + local knowledge base (RAG). Synthesis
    goes through the main failover router with the research task type, so
    the optimizer learns which provider is best at research.
    """

    REPORTS_DIR = Path("data/research")

    def __init__(self, orchestrator):
        self.orch = orchestrator
        self.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- source gathering ----------

    async def _web_search(self, query: str, k: int = 6) -> list[ResearchSource]:
        try:
            result = await self.orch.toolbox.execute(
                "web_search", f'{{"query": "{query.replace(chr(34), "")}", "max_results": {k}}}'
            )
        except Exception:
            return []
        sources = []
        for line in result.splitlines():
            line = line.strip()
            if line.startswith("- "):
                body = line[2:]
                # format: "Title — https://url\n  snippet" (web skill output)
                for sep in (" — ", " - ", " —", " -"):
                    if sep in body:
                        title_part, rest = body.split(sep, 1)
                        url = ""
                        snippet = ""
                        if "http" in rest:
                            parts = rest.split(maxsplit=1)
                            url = parts[0].strip()
                            snippet = parts[1].strip() if len(parts) > 1 else ""
                        else:
                            snippet = rest.strip()
                        if url.startswith("http"):
                            sources.append(
                                ResearchSource(
                                    title=title_part.strip()[:120],
                                    url=url,
                                    snippet=snippet[:300],
                                    origin="web",
                                )
                            )
                        break
        return sources[:k]

    def _kb_search(self, query: str, k: int = 4) -> list[ResearchSource]:
        sources = []
        try:
            results = self.orch.knowledge_store.search(query, k=k)
            for r in results:
                if r.get("score", 0) > 0.3 and r.get("text"):
                    sources.append(
                        ResearchSource(
                            title=Path(r.get("source", "local document")).name,
                            url=f"local://{r.get('source', '')}",
                            snippet=r["text"][:300],
                            origin="knowledge",
                        )
                    )
        except Exception:
            pass
        return sources

    # ---------- synthesis ----------

    async def research(
        self, topic: str, depth: int = 1, save_report: bool = True
    ) -> ResearchReport:
        """Gather sources on a topic, synthesize with citations, save markdown."""
        sources: list[ResearchSource] = []

        # Round 1: main query
        sources.extend(await self._web_search(topic, k=5))
        sources.extend(self._kb_search(topic, k=3))

        # Round 2 (depth >= 2): one follow-up angle based on what we found
        if depth >= 2:
            followup = f"{topic} latest developments comparison"
            sources.extend(await self._web_search(followup, k=3))

        # Dedup by url
        seen = set()
        unique = []
        for s in sources:
            if s.url not in seen:
                seen.add(s.url)
                unique.append(s)
        sources = unique[:9]

        if not sources:
            return ResearchReport(
                topic=topic, summary="No sources found for this topic.", provider="none"
            )

        # Build the synthesis prompt with numbered sources
        src_lines = []
        for i, s in enumerate(sources, 1):
            src_lines.append(f"[{i}] {s.title}\n    URL: {s.url}\n    {s.snippet}")
        prompt = (
            f"Research topic: {topic}\n\n"
            f"Sources:\n"
            + "\n".join(src_lines)
            + "\n\nSynthesize a research brief with: (1) a 2-3 sentence executive summary, "
            "(2) key findings as bullet points, citing sources as [n] inline, "
            "(3) contradictions or gaps between sources, (4) a confidence note. "
            "Be factual; only use the provided sources."
        )

        collected = []
        provider = ""
        try:
            from core.router import AllProvidersDown

            async for name, chunk in self.orch.router.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
                task_type="research",
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
                    provider = name
        except AllProvidersDown as exc:
            return ResearchReport(topic=topic, summary=f"research failed: {exc}")

        synthesis = "".join(collected).strip()
        report = ResearchReport(topic=topic, summary=synthesis, sources=sources, provider=provider)

        if save_report and synthesis:
            report.report_path = self._save_markdown(report)
        self.orch.event_bus.publish(
            "research_complete",
            {
                "topic": topic,
                "sources": len(sources),
                "provider": provider,
                "report": report.report_path,
            },
        )
        return report

    # ---------- persistence ----------

    def _save_markdown(self, report: ResearchReport) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        slug = "".join(c if c.isalnum() else "_" for c in report.topic.lower())[:40].strip("_")
        path = self.REPORTS_DIR / f"research_{slug}_{stamp}.md"
        lines = [
            f"# Research: {report.topic}",
            "",
            f"*Generated {time.strftime('%Y-%m-%d %H:%M')} via {report.provider}*",
            "",
            report.summary,
            "",
            "## Sources",
            "",
        ]
        for i, s in enumerate(report.sources, 1):
            lines.append(f"{i}. [{s.title}]({s.url}) — {s.origin}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

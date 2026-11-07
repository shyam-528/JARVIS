from __future__ import annotations

import time
from pathlib import Path


class DataAnalyst:
    """Analysis Agent 2.0: loads CSV/JSON data, computes stats, renders
    matplotlib charts, and produces a markdown + PNG analysis report.

    Chart rendering runs through the existing code_exec_python sandbox, so
    matplotlib failures degrade gracefully to text-only analysis.
    """

    REPORTS_DIR = Path("data/analysis")

    def __init__(self, orchestrator):
        self.orch = orchestrator
        self.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    async def _exec_python(self, code: str, timeout: int = 60) -> str:
        try:
            return await self.orch.toolbox.execute(
                "code_exec_python", f'{{"code": {_json_str(code)}, "timeout": {timeout}}}'
            )
        except Exception as exc:
            return f"error: {exc}"

    @staticmethod
    def _safe_path(path: Path) -> str:
        """Forward-slash absolute path, safe to embed in generated Python code."""
        return str(path.resolve()).replace("\\", "/").replace('"', '\\"')

    async def analyze_csv(self, path: str, question: str = "") -> str:
        """Full pipeline: load CSV -> stats -> charts -> LLM insights -> report."""
        p = Path(path)
        if not p.exists():
            return f"error: file not found: {path}"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        slug = "".join(c if c.isalnum() else "_" for c in p.stem.lower())[:30]
        p_safe = self._safe_path(p)
        out_dir = self._safe_path(self.REPORTS_DIR)

        # 1) descriptive stats via pandas
        stats_code = (
            "import pandas as pd\n"
            f'df = pd.read_csv(r"{p_safe}")\n'
            'print("shape:", df.shape)\n'
            'print("columns:", list(df.columns))\n'
            "print(df.describe(include='all').to_string())\n"
            'print("missing:", df.isna().sum().to_dict())\n'
        )
        stats = await self._exec_python(stats_code)
        if stats.startswith("error"):
            return f"stats failed: {stats}"

        # 2) chart(s) via matplotlib — plain concatenation, no nested quotes
        title = (question or p.stem).replace("'", "").replace('"', "")[:50]
        chart_code = (
            "import matplotlib\nmatplotlib.use('Agg')\n"
            "import pandas as pd\nimport matplotlib.pyplot as plt\n\n"
            'df = pd.read_csv(r"' + p_safe + '")\n'
            "num = df.select_dtypes(include='number')\n"
            "outs = []\n"
            "if not num.empty:\n"
            "    for ci, col in enumerate(list(num.columns)[:3]):\n"
            "        fig, ax = plt.subplots(figsize=(8, 4))\n"
            "        if df[col].nunique() <= 12:\n"
            "            df[col].value_counts().plot(kind='bar', ax=ax, color='#4da3ff')\n"
            "        else:\n"
            "            df[col].hist(ax=ax, bins=25, color='#4da3ff')\n"
            "        ax.set_title('" + title + " - ' + str(col))\n"
            "        ax.tick_params(axis='x', labelrotation=30)\n"
            "        plt.tight_layout()\n"
            '        outp = r"'
            + out_dir
            + "/chart_"
            + slug
            + "_"
            + stamp
            + '_" + str(ci) + ".png"\n'
            "        plt.savefig(outp, dpi=110)\n"
            "        plt.close(fig)\n"
            "        outs.append(outp)\n"
            "    print('charts:', outs)\n"
            "else:\n"
            "    print('charts: []')\n"
        )
        charts = await self._exec_python(chart_code)
        chart_paths = []
        for token in (
            charts.replace("'", " ").replace("[", " ").replace("]", " ").replace(",", " ").split()
        ):
            if "chart_" in token and token.endswith(".png"):
                chart_paths.append(token)

        # 3) LLM insights with the research/analysis brain
        prompt = (
            f"Data file: {p.name}\nQuestion: {question or 'general analysis'}\n\n"
            f"Descriptive stats (pandas):\n{stats[:3500]}\n\n"
            "As the ANALYSIS AGENT: give (1) a 2-sentence overview of this dataset, "
            "(2) 3-5 concrete insights with numbers, (3) data quality warnings, "
            "(4) one recommended action. Be specific, not generic."
        )
        collected = []
        try:
            async for _name, chunk in self.orch.router.chat(
                [{"role": "user", "content": prompt}],
                tools=None,
                task_type="analysis",
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
        except Exception:
            pass
        insights = "".join(collected).strip() or "(LLM insights unavailable)"

        # 4) markdown report
        report_path = self.REPORTS_DIR / f"analysis_{slug}_{stamp}.md"
        lines = [
            f"# Analysis: {p.name}",
            "",
            f"*{time.strftime('%Y-%m-%d %H:%M')} · question: {question or 'general'}*",
            "",
            "## Insights",
            "",
            insights,
            "",
            "## Descriptive stats",
            "",
            "```",
            stats[:2500],
            "```",
        ]
        if chart_paths:
            lines += ["", "## Charts", ""]
            for cp in chart_paths:
                # reports live in data/analysis; charts too — use bare filename
                lines.append(f"![chart]({Path(cp).name})")
        report_path.write_text("\n".join(lines), encoding="utf-8")

        self.orch.event_bus.publish(
            "analysis_complete",
            {
                "file": str(p),
                "charts": len(chart_paths),
                "report": str(report_path),
            },
        )
        return (
            f"analysis complete: {len(chart_paths)} charts, report saved to "
            f"{report_path}\n\n{insights[:600]}"
        )


def _json_str(s: str) -> str:
    import json

    return json.dumps(s)

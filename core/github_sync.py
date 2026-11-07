from __future__ import annotations

import contextlib
import time

import httpx


class GitHubSync:
    """Project management layer: repos, issues, PRs, CI status, standups.

    Uses the public GitHub REST API with GITHUB_TOKEN (optional; higher rate
    limits) — works unauthenticated for public repos at 60 req/h.
    """

    API = "https://api.github.com"

    def __init__(self, orchestrator):
        self.orch = orchestrator
        import os

        self.token = os.getenv("GITHUB_TOKEN", "")
        self.headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    # ---------- low level ----------

    async def _get(self, path: str) -> dict | list | None:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(self.API + path, headers=self.headers)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code == 403:
                    return {"error": "rate limited — set GITHUB_TOKEN in .env for higher limits"}
                return {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            return {"error": str(exc)}

    @staticmethod
    def _parse_repo(repo: str) -> tuple[str, str]:
        """Accepts 'owner/repo' or a full github URL."""
        repo = repo.strip().rstrip("/").removesuffix(".git")
        if repo.startswith("http"):
            parts = [p for p in repo.split("/") if p]
            return parts[-2], parts[-1]
        if "/" in repo:
            owner, name = repo.split("/", 1)
            return owner, name
        raise ValueError("repo must be 'owner/repo' or a GitHub URL")

    # ---------- reads ----------

    async def repo_overview(self, repo: str) -> str:
        owner, name = self._parse_repo(repo)
        data = await self._get(f"/repos/{owner}/{name}")
        if not data or "error" in (data or {}):
            return f"error: {(data or {}).get('error', 'repo not found')}"
        lines = [
            f"{data['full_name']} — {data.get('description', '')}",
            f"stars: {data['stargazers_count']} | forks: {data['forks_count']} | "
            f"open issues: {data['open_issues_count']}",
            f"branch: {data.get('default_branch', '-')} | updated: {data.get('pushed_at', '-')[:10]}",
            f"language: {data.get('language', '-')} | {data.get('html_url', '')}",
        ]
        return "\n".join(lines)

    async def list_issues(self, repo: str, state: str = "open", limit: int = 10) -> str:
        owner, name = self._parse_repo(repo)
        data = await self._get(f"/repos/{owner}/{name}/issues?state={state}&per_page={limit}")
        if not isinstance(data, list):
            return f"error: {(data or {}).get('error', 'failed')}"
        if not data:
            return f"no {state} issues in {owner}/{name}"
        lines = [f"{state} issues in {owner}/{name} ({len(data)}):"]
        for i in data[:limit]:
            kind = "PR" if "pull_request" in i else "issue"
            labels = ",".join(lb["name"] for lb in i.get("labels", []))
            extra = f" [{labels}]" if labels else ""
            lines.append(
                f"- #{i['number']} ({kind}) {i['title'][:70]}{extra} — {i['user']['login']}"
            )
        return "\n".join(lines)

    async def list_prs(self, repo: str, state: str = "open", limit: int = 10) -> str:
        owner, name = self._parse_repo(repo)
        data = await self._get(f"/repos/{owner}/{name}/pulls?state={state}&per_page={limit}")
        if not isinstance(data, list):
            return f"error: {(data or {}).get('error', 'failed')}"
        if not data:
            return f"no {state} PRs in {owner}/{name}"
        lines = [f"{state} PRs in {owner}/{name} ({len(data)}):"]
        for pr in data[:limit]:
            lines.append(
                f"- #{pr['number']} {pr['title'][:70]} — {pr['user']['login']} ({pr.get('draft', False) and 'draft' or 'ready'})"
            )
        return "\n".join(lines)

    # ---------- standup generation ----------

    async def standup(self, repos: list[str], username: str = "") -> str:
        """Generate a daily standup: recent activity across repos."""
        sections = [f"**Daily Standup — {time.strftime('%Y-%m-%d')}**", ""]
        for repo in repos[:5]:
            owner, name = self._parse_repo(repo)
            repo_disp = f"{owner}/{name}"

            issues = await self._get(
                f"/repos/{owner}/{name}/issues?state=all&sort=updated&per_page=8"
            )
            pushes = await self._get(f"/repos/{owner}/{name}/commits?per_page=5")
            prs = await self._get(f"/repos/{owner}/{name}/pulls?state=all&sort=updated&per_page=5")

            sections.append(f"### {repo_disp}")

            if isinstance(pushes, list) and pushes:
                latest = pushes[0]
                sections.append(
                    f"- Latest commit: {latest['commit']['message'].splitlines()[0][:70]} "
                    f"({(latest['commit']['author']['name'],)})"
                )
                sections.append(f"- {len(pushes)} recent commits")
            if isinstance(prs, list) and prs:
                for pr in prs[:3]:
                    mark = "OPEN" if pr["state"] == "open" else "merged"
                    sections.append(f"- PR #{pr['number']} [{mark}] {pr['title'][:60]}")
            if isinstance(issues, list) and issues:
                for i in issues[:3]:
                    if "pull_request" not in i:
                        sections.append(f"- Issue #{i['number']} ({i['state']}): {i['title'][:60]}")
            if all(not isinstance(x, list) for x in (issues, pushes, prs)):
                sections.append("- (no accessible activity — check repo name / token)")
            sections.append("")

        self.orch.event_bus.publish("standup_generated", {"repos": repos})
        return "\n".join(sections)

    # ---------- CI failure watch ----------

    async def check_ci(self, repo: str) -> str:
        owner, name = self._parse_repo(repo)
        data = await self._get(f"/repos/{owner}/{name}/actions/runs?per_page=5")
        if not isinstance(data, dict) or "workflow_runs" not in (data or {}):
            return f"error: {(data or {}).get('error', 'no CI data (Actions disabled?)')}"
        runs = data["workflow_runs"]
        lines = [f"CI status for {owner}/{name} (last {len(runs)} runs):"]
        failures = 0
        for r in runs:
            mark = {"success": "✅", "failure": "❌", "in_progress": "⏳"}.get(
                r["status"], r["status"]
            )
            if r["conclusion"] == "failure":
                failures += 1
            lines.append(
                f"  {mark} {r['name']} — {r['head_branch']} — {r['conclusion'] or r['status']} ({r['created_at'][:10]})"
            )
        if failures:
            lines.append(f"⚠ {failures} failing runs — alerting via notify")
            with contextlib.suppress(Exception):
                await self.orch.toolbox.execute(
                    "notify_alert",
                    f'{{"message": "CI failures on {repo}: check Actions", '
                    '"title": "JARVIS CI Watch"}',
                )
        return "\n".join(lines)

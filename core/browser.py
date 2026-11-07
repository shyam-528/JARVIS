from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


class BrowserManager:
    """Manages Playwright browser with persistent profile for cookies/session persistence."""

    def __init__(self, profile_dir: Path | None = None, headless: bool = True):
        self.profile_dir = profile_dir or Path.home() / ".jarvis_browser"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless

        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._default_context: BrowserContext | None = None

    async def start(self):
        """Start the browser and create default context."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            )

    async def get_context(self, name: str = "default") -> BrowserContext:
        """Get or create a persistent browser context."""
        if self._browser is None:
            await self.start()

        if name not in self._contexts:
            context = await self._browser.new_context(
                storage_state=self._get_storage_path(name)
                if (self.profile_dir / name).exists()
                else None,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                permissions=["geolocation", "notifications"],
            )

            # Save storage state on close
            async def save_storage():
                try:
                    state = await context.storage_state()
                    self._save_storage(name, state)
                except Exception:
                    pass

            context.on("close", lambda: asyncio.create_task(save_storage()))
            self._contexts[name] = context

        return self._contexts[name]

    def _get_storage_path(self, name: str) -> Path:
        return self.profile_dir / name / "storage.json"

    def _save_storage(self, name: str, state: dict):
        path = self._get_storage_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    async def new_page(self, context_name: str = "default") -> Page:
        """Create a new page in the specified context."""
        context = await self.get_context(context_name)
        return await context.new_page()

    async def close_context(self, name: str):
        """Close a specific context and save its state."""
        if name in self._contexts:
            context = self._contexts[name]
            try:
                state = await context.storage_state()
                self._save_storage(name, state)
            except Exception:
                pass
            await context.close()
            del self._contexts[name]

    async def close(self):
        """Close all contexts and the browser."""
        for name in list(self._contexts.keys()):
            await self.close_context(name)
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()


# Global browser manager instance
_browser_manager: BrowserManager | None = None


async def get_browser_manager(headless: bool = True) -> BrowserManager:
    """Get or create the global browser manager."""
    global _browser_manager
    if _browser_manager is None:
        _browser_manager = BrowserManager(headless=headless)
    return _browser_manager


async def browser_navigate(
    url: str, context_name: str = "default", wait_until: str = "networkidle"
) -> str:
    """Navigate to a URL."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    await page.goto(url, wait_until=wait_until, timeout=30000)
    return f"navigated to {url} (title: {await page.title()})"


async def browser_click(
    selector: str, context_name: str = "default", nth: int = 0, timeout: int = 10000
) -> str:
    """Click an element."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    element = page.locator(selector).nth(nth)
    await element.wait_for(state="visible", timeout=timeout)
    await element.click()
    return f"clicked {selector}"


async def browser_type(
    selector: str, text: str, context_name: str = "default", delay: int = 50, timeout: int = 10000
) -> str:
    """Type text into an element."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    element = page.locator(selector)
    await element.wait_for(state="visible", timeout=timeout)
    await element.fill("")
    await element.type(text, delay=delay)
    return f"typed into {selector}"


async def browser_fill_form(form_data: dict, context_name: str = "default") -> str:
    """Fill multiple form fields at once."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    results = []
    for selector, value in form_data.items():
        try:
            await page.fill(selector, value)
            results.append(f"{selector}: filled")
        except Exception as e:
            results.append(f"{selector}: error - {e}")
    return "Form fill results:\n" + "\n".join(results)


async def browser_extract(
    selector: str, attr: str = "textContent", context_name: str = "default"
) -> str:
    """Extract data from elements."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    elements = page.locator(selector)
    count = await elements.count()
    if count == 0:
        return "no elements found"

    results = []
    for i in range(min(count, 20)):
        element = elements.nth(i)
        if attr == "textContent":
            value = await element.text_content()
        else:
            value = await element.get_attribute(attr)
        results.append(f"[{i}] {value}")
    return "\n".join(results)


async def browser_screenshot(
    path: str = "", context_name: str = "default", full_page: bool = False
) -> str:
    """Take a screenshot."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    if not path:
        path = f"screenshot_{int(asyncio.get_event_loop().time())}.png"
    path_obj = Path(path).expanduser().resolve()
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path_obj), full_page=full_page)
    return f"screenshot saved: {path_obj}"


async def browser_pdf(path: str, context_name: str = "default") -> str:
    """Save page as PDF."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    path_obj = Path(path).expanduser().resolve()
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    await page.pdf(path=str(path_obj))
    return f"PDF saved: {path_obj}"


async def browser_wait(
    selector: str, state: str = "visible", timeout: int = 30000, context_name: str = "default"
) -> str:
    """Wait for an element to reach a certain state."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    await page.locator(selector).wait_for(state=state, timeout=timeout)
    return f"element {selector} is {state}"


async def browser_eval(script: str, context_name: str = "default") -> str:
    """Execute JavaScript in the page context."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    result = await page.evaluate(script)
    return str(result)


async def browser_download(selector: str, save_path: str, context_name: str = "default") -> str:
    """Click element and save download."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    path_obj = Path(save_path).expanduser().resolve()
    path_obj.parent.mkdir(parents=True, exist_ok=True)

    async with page.expect_download() as download_info:
        await page.click(selector)
    download = await download_info.value
    await download.save_as(str(path_obj))
    return f"downloaded: {path_obj}"


async def browser_cookies(domain: str = "", context_name: str = "default") -> str:
    """Get cookies for a domain."""
    manager = await get_browser_manager()
    context = await manager.get_context(context_name)
    cookies = await context.cookies()
    if domain:
        cookies = [c for c in cookies if domain in c["domain"]]
    return json.dumps(cookies, indent=2)


async def browser_storage(
    key: str = "", type: str = "localStorage", context_name: str = "default"
) -> str:
    """Access localStorage or sessionStorage."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    if type == "localStorage":
        script = (
            f"return localStorage.getItem('{key}')"
            if key
            else "return Object.assign({}, localStorage)"
        )
    else:
        script = (
            f"return sessionStorage.getItem('{key}')"
            if key
            else "return Object.assign({}, sessionStorage)"
        )
    result = await page.evaluate(script)
    return json.dumps(result, indent=2) if isinstance(result, dict) else str(result)


async def browser_set_storage(
    key: str, value: str, type: str = "localStorage", context_name: str = "default"
) -> str:
    """Set localStorage or sessionStorage."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    if type == "localStorage":
        await page.evaluate(f"localStorage.setItem('{key}', '{value}')")
    else:
        await page.evaluate(f"sessionStorage.setItem('{key}', '{value}')")
    return f"set {type}[{key}] = {value}"


async def browser_clear_storage(type: str = "localStorage", context_name: str = "default") -> str:
    """Clear localStorage or sessionStorage."""
    manager = await get_browser_manager()
    page = await manager.new_page("default")
    if type == "localStorage":
        await page.evaluate("localStorage.clear()")
    else:
        await page.evaluate("sessionStorage.clear()")
    return f"cleared {type}"


async def browser_login(site: str, credentials_ref: str, context_name: str = "default") -> str:
    """Auto-login to a site using saved credentials."""
    # This would require a credentials store - placeholder for now
    return f"login for {site} requires credentials store (not implemented)"


def register_browser(box) -> None:
    """Register browser tools - placeholder for ToolBox registration."""
    pass

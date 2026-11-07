"""Browser automation tools: navigate, click, type, extract, screenshot, PDF,
storage/cookies. Fail-soft — handlers return error strings when the browser
is unavailable instead of raising into the agent loop.
"""

from __future__ import annotations

from core.browser import (
    browser_clear_storage,
    browser_click,
    browser_cookies,
    browser_download,
    browser_eval,
    browser_extract,
    browser_fill_form,
    browser_navigate,
    browser_pdf,
    browser_screenshot,
    browser_set_storage,
    browser_storage,
    browser_type,
    browser_wait,
)
from core.tools import Tool, ToolBox


def register_browser(box: ToolBox) -> None:
    box.register(
        Tool(
            name="browser_navigate",
            description="Navigate to a URL in the browser",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "context_name": {"type": "string", "default": "default"},
                    "wait_until": {"type": "string", "default": "networkidle"},
                },
                "required": ["url"],
            },
            handler=browser_navigate,
        )
    )
    box.register(
        Tool(
            name="browser_click",
            description="Click an element on the page",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "context_name": {"type": "string", "default": "default"},
                    "nth": {"type": "integer", "default": 0},
                    "timeout": {"type": "integer", "default": 10000},
                },
                "required": ["selector"],
            },
            handler=browser_click,
        )
    )
    box.register(
        Tool(
            name="browser_type",
            description="Type text into an input element",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "context_name": {"type": "string", "default": "default"},
                    "delay": {"type": "integer", "default": 50},
                    "timeout": {"type": "integer", "default": 10000},
                },
                "required": ["selector", "text"],
            },
            handler=browser_type,
        )
    )
    box.register(
        Tool(
            name="browser_fill_form",
            description="Fill multiple form fields at once",
            parameters={
                "type": "object",
                "properties": {
                    "form_data": {"type": "object", "additionalProperties": {"type": "string"}},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["form_data"],
            },
            handler=browser_fill_form,
        )
    )
    box.register(
        Tool(
            name="browser_extract",
            description="Extract data from page elements (text, attributes, etc.)",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "attr": {"type": "string", "default": "textContent"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["selector"],
            },
            handler=browser_extract,
        )
    )
    box.register(
        Tool(
            name="browser_screenshot",
            description="Take a screenshot of the current page",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": ""},
                    "context_name": {"type": "string", "default": "default"},
                    "full_page": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            handler=browser_screenshot,
        )
    )
    box.register(
        Tool(
            name="browser_pdf",
            description="Save the current page as PDF",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["path"],
            },
            handler=browser_pdf,
        )
    )
    box.register(
        Tool(
            name="browser_wait",
            description="Wait for an element to reach a certain state",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "state": {"type": "string", "default": "visible"},
                    "timeout": {"type": "integer", "default": 30000},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["selector"],
            },
            handler=browser_wait,
        )
    )
    box.register(
        Tool(
            name="browser_eval",
            description="Execute JavaScript in the page context",
            parameters={
                "type": "object",
                "properties": {
                    "script": {"type": "string"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["script"],
            },
            handler=browser_eval,
        )
    )
    box.register(
        Tool(
            name="browser_download",
            description="Click element and save the downloaded file",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "save_path": {"type": "string"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["selector", "save_path"],
            },
            handler=browser_download,
        )
    )
    box.register(
        Tool(
            name="browser_cookies",
            description="Get cookies for a domain",
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "default": ""},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": [],
            },
            handler=browser_cookies,
        )
    )
    box.register(
        Tool(
            name="browser_storage",
            description="Access localStorage or sessionStorage",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "default": ""},
                    "type": {"type": "string", "default": "localStorage"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": [],
            },
            handler=browser_storage,
        )
    )
    box.register(
        Tool(
            name="browser_set_storage",
            description="Set localStorage or sessionStorage value",
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "type": {"type": "string", "default": "localStorage"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": ["key", "value"],
            },
            handler=browser_set_storage,
        )
    )
    box.register(
        Tool(
            name="browser_clear_storage",
            description="Clear localStorage or sessionStorage",
            parameters={
                "type": "object",
                "properties": {
                    "type": {"type": "string", "default": "localStorage"},
                    "context_name": {"type": "string", "default": "default"},
                },
                "required": [],
            },
            handler=browser_clear_storage,
        )
    )

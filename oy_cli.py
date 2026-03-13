from __future__ import annotations

import asyncio
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import diskcache
import httpx
import typer
from aws_bedrock_token_generator import provide_token  # pyright: ignore[reportMissingImports]
from openai import AsyncOpenAI, OpenAI  # pyright: ignore[reportMissingImports]

from rich.console import Console
from rich.json import JSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax

MAX_CHARS = 12000
DEFAULT_MODEL = "moonshotai.kimi-k2.5"
DEFAULT_REGION = (
    os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
)
DEFAULT_MAX_STEPS = 512
DEFAULT_MAX_TOOL_CALLS = 512
CONFIG_PATH = Path.home() / ".config" / "oy" / "config.json"
HISTORY_CACHE_PATH = Path.home() / ".cache" / "oy" / "history"
MAX_HISTORY_PER_DIR = 20

# Track if we're using Bedrock for token refresh on expiry
_using_bedrock: bool = False

SYSTEM_PROMPT = """You are oy, a tiny local coding assistant.

Work simply and directly. Follow OWASP-minded secure defaults: least privilege, minimal changes, input validation, careful file handling, and no secret exposure. Prefer the grugbrain.dev philosophy: choose boring, obvious solutions over clever abstractions.

Be concise. Keep responses to one or two CLI screens. Inspect before changing. Use the smallest change that works.

Use workspace tools (read/edit/grep/list/glob) for file operations. Reserve `bash` for builds, tests, git, and system commands only. See tool descriptions for specifics.

Use `webfetch` to fetch web pages over HTTP/HTTPS. Useful for getting up-to-date documentation, checking library references, or verifying current API details. Automatically follows redirects and truncates large responses.
"""


app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console(stderr=True, highlight=False, soft_wrap=True)
output_console = Console(highlight=False, soft_wrap=True)


class Spinner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.status: Status | None = None

    def __enter__(self) -> None:
        if not sys.stderr.isatty():
            console.print(f"[bright_black]▸[/] {self.label}")
            return
        self.status = console.status(f"[dim]{self.label}[/]", spinner="dots")
        self.status.__enter__()

    def __exit__(self, *_: object) -> None:
        if self.status:
            self.status.__exit__(None, None, None)


def render_preview(value: Any, limit: int = 72) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def render_tool_details(**details: Any) -> str:
    # Shorten common keys for denser output
    key_shortcuts = {
        "path": "p",
        "pattern": "pat",
        "command": "cmd",
        "timeout": "t",
        "offset": "off",
        "limit": "lim",
        "chars": "c",
        "old_chars": "old",
        "new_chars": "new",
        "replace_all": "all",
        "file_glob": "glob",
        "max_chars": "max",
        "url": "url",
        "n": "n",
    }
    parts = []
    for key, value in details.items():
        if value is None or value == "":
            continue
        k = key_shortcuts.get(key, key)
        # For booleans, just show key if true
        if isinstance(value, bool):
            if value:
                parts.append(k)
        else:
            preview = render_preview(value, limit=50)
            parts.append(f"{k}={preview}")
    return " ".join(parts)


def parse_tool_arguments(args_str: str) -> dict[str, Any]:
    def decode(candidate: str) -> dict[str, Any]:
        parsed = json.loads(candidate)
        parsed = json.loads(parsed) if isinstance(parsed, str) else parsed
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object")
        return parsed

    try:
        return decode(args_str)
    except (json.JSONDecodeError, ValueError) as exc:
        # Hack around duplicated return values some llms do
        mid = len(args_str) // 2

        # Hunt specifically for '{' in a generous window around the midpoint
        for i in range(max(0, mid - 15), min(len(args_str), mid + 15)):
            if args_str[i] == "{":
                try:
                    return decode(args_str[i:])
                except json.JSONDecodeError, ValueError:
                    pass

        # If no valid JSON is found starting with those brackets, raise the original error
        raise exc


def print_event(
    label: str, value: str | dict[str, Any] | None = None, border_style: str = "yellow"
) -> None:
    """Print a status event. If value is a dict, shows as JSON panel; otherwise as text."""
    if isinstance(value, dict):
        if sys.stderr.isatty():
            console.print(
                Panel.fit(
                    JSON.from_data(value),
                    title=label,
                    border_style=border_style,
                    padding=(0, 1),
                )
            )
        else:
            console.print(f"▸ {label}")
            console.print(json.dumps(value, indent=2, ensure_ascii=True))
    else:
        text = f"[bold cyan]▸[/] {label}"
        if value:
            text += f" [bold]{value}[/]"
        console.print(text)


def render_agent_output(text: str) -> None:
    if not sys.stdout.isatty():
        print(text)
        return
    stripped = text.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            output_console.print(Markdown(text))
        else:
            output_console.print(JSON.from_data(parsed))
        return
    print(text)


@dataclass
class AgentDeps:
    root: Path
    max_tool_calls: int
    tool_calls: int = 0


def clip(text: str, limit: int = MAX_CHARS) -> str:
    return (
        text
        if len(text) <= limit
        else text[:limit] + f"\n... [truncated to {limit} chars]"
    )


def print_preview(text: str, lines: int = 2) -> None:
    """Print short preview of output."""
    if not text:
        return
    preview_lines = text.splitlines()[:lines]
    for line in preview_lines:
        # Truncate to 100 chars for denser display
        console.print(f"  [bright_black]│[/] [dim]{line[:100]}[/]")


def rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.as_posix()


def fail(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def abort(message: str, code: int = 1) -> None:
    raise typer.Exit(fail(message, code))


def config_path() -> Path:
    return Path(os.environ.get("OY_CONFIG", str(CONFIG_PATH))).expanduser()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return default


def load_config() -> dict[str, str]:
    data = load_json(config_path(), {})
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, str]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def history_cache() -> diskcache.Cache:
    HISTORY_CACHE_PATH.mkdir(parents=True, exist_ok=True)
    return diskcache.Cache(str(HISTORY_CACHE_PATH))


def history_key(root: Path) -> str:
    return str(root.resolve())


def load_history(root: Path) -> list[dict[str, Any]]:
    with history_cache() as cache:
        return cache.get(history_key(root), [])


def save_history(root: Path, prompt: str, output: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt,
        "output": output,
    }
    with history_cache() as cache:
        key = history_key(root)
        history = cache.get(key, [])
        history.append(entry)
        # Keep only last N entries
        cache[key] = history[-MAX_HISTORY_PER_DIR:]


def current_model(choice: str | None) -> str:
    return (
        choice
        or os.environ.get("OY_MODEL")
        or load_config().get("model")
        or DEFAULT_MODEL
    )


def current_region(choice: str | None) -> str:
    return (
        choice
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )


def bedrock_base_url(region: str) -> str:
    return f"https://bedrock-mantle.{region}.api.aws/v1"


def has_aws_credentials() -> bool:
    """Check if AWS credentials are available via standard config or environment."""
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    if os.environ.get("AWS_PROFILE"):
        return shutil.which("aws") is not None
    aws_dir = Path.home() / ".aws"
    # Check for credentials file OR config file (SSO stores in config)
    has_config = (aws_dir / "credentials").exists() or (aws_dir / "config").exists()
    return has_config and shutil.which("aws") is not None


def auto_configure_bedrock() -> bool:
    """Auto-configure Bedrock if AWS is available and OpenAI key is not set.

    Sets OPENAI_API_KEY and OPENAI_BASE_URL from Bedrock token generation.
    Returns True if successful, False otherwise.
    """
    global _using_bedrock
    if os.environ.get("OPENAI_API_KEY"):
        return True
    if not has_aws_credentials():
        return False
    region = current_region(None)
    try:
        token = provide_token(region=region)
        os.environ["OPENAI_API_KEY"] = token
        os.environ["OPENAI_BASE_URL"] = bedrock_base_url(region)
        _using_bedrock = True
        return True
    except Exception:  # noqa: BLE001
        return False


def refresh_bedrock_token() -> bool:
    """Refresh Bedrock token. Returns True if successful."""
    global _using_bedrock
    if not _using_bedrock:
        return False
    region = current_region(None)
    try:
        token = provide_token(region=region)
        os.environ["OPENAI_API_KEY"] = token
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_api_env() -> bool:
    """Ensure API environment is configured, preferring OpenAI, falling back to Bedrock.

    Returns True if configured successfully, False otherwise.
    """
    if os.environ.get("OPENAI_API_KEY"):
        return True
    return auto_configure_bedrock()


def get_openai_client(async_: bool = False) -> OpenAI | AsyncOpenAI:
    """Get an OpenAI client (sync or async)."""
    ensure_api_env()
    cls = AsyncOpenAI if async_ else OpenAI
    return cls(
        api_key=str(os.environ["OPENAI_API_KEY"]),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        max_retries=3,
    )


def get_api_client() -> httpx.AsyncClient:
    ensure_api_env()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        timeout=60.0,
        follow_redirects=True,
    )


def require_api_env() -> None:
    if not ensure_api_env():
        abort(
            "missing required environment: OPENAI_API_KEY "
            "(or AWS credentials for automatic Bedrock setup)"
        )


def require_tools(*tools: str) -> None:
    hints = {
        "aws": "Install the AWS CLI and configure a profile or credentials.",
        "bash": "Install bash with your system package manager.",
        "patch": "Install patch with your system package manager.",
        "rg": "`rg` is expected from the `ripgrep` dependency or a system install. Try `uv sync`, `pip install ripgrep`, `brew install ripgrep`, or your distro package manager.",
    }
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        notes = "\n".join(
            f"- {tool}: {hints.get(tool, f'Install `{tool}` and make sure it is on PATH.')}"
            for tool in missing
        )
        abort(f"required tools missing:\n{notes}")


def require_runtime() -> None:
    require_api_env()
    require_tools("bash", "patch", "rg")


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    live: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=not live,
            timeout=max(timeout, 1),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"command timed out after {timeout} seconds") from exc


def aws_command(*parts: str, region: str | None = None) -> list[str]:
    command = ["aws"]
    if os.environ.get("AWS_PROFILE"):
        command.extend(["--profile", str(os.environ["AWS_PROFILE"])])
    if region:
        command.extend(["--region", region])
    command.extend(parts)
    return command


def generate_bedrock_token(region: str) -> str:
    with Spinner("generating Bedrock token"):
        return provide_token(region=region)


def resolve_path(root: Path, raw: str) -> Path:
    path = (root / raw).resolve()
    if path != root and root not in path.parents:
        return root / Path(raw).name
    return path


def list_tool(root: Path, path: str = ".", limit: int = 200) -> str:
    target = resolve_path(root, path)
    if not target.is_dir():
        raise ValueError("path is not a directory")
    items = [
        rel(root, item) + ("/" if item.is_dir() else "")
        for item in sorted(target.iterdir())[: max(limit, 1)]
    ]
    output = clip("\n".join(items) or "<empty directory>")
    print_preview(output, lines=1)
    return output


def read_tool(root: Path, path: str, offset: int = 1, limit: int = 200) -> str:
    target = resolve_path(root, path)
    if target.is_dir():
        return list_tool(root, path, limit)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(offset, 1) - 1
    body = [
        f"{i + 1}: {line}"
        for i, line in enumerate(lines[start : start + max(limit, 1)], start=start)
    ]
    return clip("\n".join(body) or "<empty file>")


def write_tool(root: Path, path: str, content: str) -> str:
    target = resolve_path(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    result = f"wrote {rel(root, target)} ({len(content)} chars)"
    print_preview(result, lines=1)
    return result


def edit_tool(
    root: Path, path: str, old_text: str, new_text: str, replace_all: bool = False
) -> str:
    """Replace text in an existing workspace file using fuzzy patch matching."""
    if not old_text:
        raise ValueError("old_text must not be empty")
    target = resolve_path(root, path)
    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_text)
    if count == 0:
        raise ValueError("old_text not found")
    if count > 1 and not replace_all:
        raise ValueError("old_text matched multiple locations; set replace_all=true")

    if replace_all:
        # Replace all occurrences directly
        target.write_text(text.replace(old_text, new_text), encoding="utf-8")
        result = f"edited {rel(root, target)} ({count} replacement{'s' if count != 1 else ''})"
        print_preview(result, lines=1)
        return result

    # Build a unified diff for the first occurrence and apply with fuzzy matching
    import difflib

    idx = text.find(old_text)
    # Get text before and after the change
    before = text[:idx]
    after = text[idx + len(old_text) :]
    new_content = before + new_text + after

    # Generate unified diff
    fromfile = f"a/{path}"
    tofile = f"b/{path}"
    diff_lines = list(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="\n",
        )
    )
    patch_text = "".join(diff_lines)
    if not patch_text:
        # No changes needed
        result = f"edited {rel(root, target)} (no changes)"
        print_preview(result, lines=1)
        return result

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(patch_text)
        patch_file = handle.name
    try:
        result_proc = run(
            [
                "patch",
                "--strip=1",
                "--directory",
                str(root),
                "--input",
                patch_file,
                "--forward",
                "--batch",
                "--fuzz=3",
            ]
        )
    finally:
        Path(patch_file).unlink(missing_ok=True)

    if result_proc.returncode != 0:
        raise ValueError(clip(f"patch failed: {result_proc.stderr.strip()}"))

    result = f"edited {rel(root, target)} (1 replacement)"
    print_preview(result, lines=1)
    return result


def patch_tool(root: Path, patch_text: str) -> str:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(patch_text)
        patch_file = handle.name
    try:
        result = run(
            [
                "patch",
                "--strip=0",
                "--directory",
                str(root),
                "--input",
                patch_file,
                "--forward",
                "--batch",
            ]
        )
    finally:
        Path(patch_file).unlink(missing_ok=True)
    output = f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
    if result.returncode != 0:
        raise ValueError(clip(output))
    print_preview(output, lines=1)
    return clip(output)


def bash_tool(root: Path, command: str, timeout_seconds: int = 120) -> str:
    result = run(["bash", "-lc", command], cwd=root, timeout=timeout_seconds)
    output = f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
    print_preview(output, lines=3)
    return clip(output)


def grep_tool(
    root: Path, pattern: str, path: str = ".", file_glob: str | None = None
) -> str:
    command = [
        "rg",
        "--line-number",
        "--column",
        "--color",
        "never",
        "--hidden",
        "--glob",
        "!.git",
    ]
    if file_glob:
        command.extend(["--glob", file_glob])
    command.extend([pattern, str(resolve_path(root, path))])
    result = run(command)
    if result.returncode not in (0, 1):
        raise ValueError(result.stderr.strip() or "rg failed")
    output = result.stdout.strip() or "<no matches>"
    print_preview(output, lines=3)
    return clip(output)


def glob_tool(root: Path, pattern: str, path: str = ".") -> str:
    base = resolve_path(root, path)
    items = [
        rel(root, match) + ("/" if match.is_dir() else "")
        for match in sorted(base.glob(pattern))[:200]
    ]
    output = "\n".join(items) or "<no matches>"
    print_preview(output, lines=1)
    return clip(output)


def webfetch_tool(url: str, max_chars: int = MAX_CHARS) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webfetch only supports http and https")
    with Spinner("fetching web content"):
        with httpx.Client(follow_redirects=True, timeout=20.0) as http:
            response = http.get(url)
            response.raise_for_status()
    prefix = f"url: {response.url}\nstatus: {response.status_code}\ncontent-type: {response.headers.get('content-type', '')}\n\n"
    output = clip(prefix + response.text, max_chars)
    print_preview(output, lines=1)
    return output


def note_tool_call(deps: AgentDeps, name: str, details: str = "") -> None:
    if deps.tool_calls >= deps.max_tool_calls:
        raise ValueError(
            f"reached max tool calls ({deps.max_tool_calls}) without a final response"
        )
    deps.tool_calls += 1
    # Denser format: highlight tool name, dim details
    if name == "bash" and details.startswith("cmd="):
        # Special handling for bash commands with syntax highlighting
        cmd = details[4:]  # Remove "cmd=" prefix
        if sys.stderr.isatty() and cmd:
            # Show with syntax highlighting, compact single line style
            console.print(f"[bright_black]▸[/] [bold]{name}[/]", highlight=False)
            syntax = Syntax(cmd, "bash", theme="native", word_wrap=True)
            console.print(syntax)
        else:
            console.print(
                f"[bright_black]▸[/] [bold]{name}[/] [dim]{cmd}[/]", highlight=False
            )
    elif details:
        console.print(
            f"[bright_black]▸[/] [bold]{name}[/] [dim]{details}[/]", highlight=False
        )
    else:
        console.print(f"[bright_black]▸[/] [bold]{name}[/]", highlight=False)


# Tool implementations that take AgentDeps directly
def tool_write(deps: AgentDeps, path: str, content: str) -> str:
    """Create or overwrite a file in the workspace."""
    note_tool_call(deps, "write", render_tool_details(path=path, chars=len(content)))
    return write_tool(deps.root, path, content)


def tool_edit(
    deps: AgentDeps,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    """Replace text in an existing workspace file using fuzzy patch matching for resilience."""
    note_tool_call(
        deps,
        "edit",
        render_tool_details(
            path=path,
            old_chars=len(old_text),
            new_chars=len(new_text),
            replace_all=replace_all,
        ),
    )
    return edit_tool(deps.root, path, old_text, new_text, replace_all)


def tool_patch(deps: AgentDeps, patch_text: str) -> str:
    """Apply a unified diff inside the workspace."""
    note_tool_call(deps, "patch", render_tool_details(chars=len(patch_text)))
    return patch_tool(deps.root, patch_text)


def tool_list(deps: AgentDeps, path: str = ".", limit: int = 200) -> str:
    """List files and directories in a workspace directory. Use this instead of `bash` with `ls`."""
    note_tool_call(deps, "list", render_tool_details(path=path, limit=limit))
    return list_tool(deps.root, path, limit)


def tool_bash(deps: AgentDeps, command: str, timeout_seconds: int = 120) -> str:
    """Last resort: run shell commands for builds, tests, git, package managers, or other real terminal tasks."""
    note_tool_call(
        deps,
        "bash",
        render_tool_details(command=command, timeout=timeout_seconds),
    )
    return bash_tool(deps.root, command, timeout_seconds)


def tool_read(deps: AgentDeps, path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a file in the workspace. Use this instead of `cat`, `head`, or `tail`."""
    note_tool_call(
        deps, "read", render_tool_details(path=path, offset=offset, limit=limit)
    )
    return read_tool(deps.root, path, offset, limit)


def tool_grep(
    deps: AgentDeps,
    pattern: str,
    path: str = ".",
    file_glob: str | None = None,
) -> str:
    """Search workspace file contents with ripgrep. Use this instead of `grep` or `rg` in bash."""
    note_tool_call(
        deps,
        "grep",
        render_tool_details(pattern=pattern, path=path, glob=file_glob),
    )
    return grep_tool(deps.root, pattern, path, file_glob)


def tool_glob(deps: AgentDeps, pattern: str, path: str = ".") -> str:
    """Find files or directories with glob patterns. Use this instead of `find` in bash."""
    note_tool_call(deps, "glob", render_tool_details(pattern=pattern, path=path))
    return glob_tool(deps.root, pattern, path)


def tool_webfetch(deps: AgentDeps, url: str, max_chars: int = MAX_CHARS) -> str:
    """Fetch a web page over HTTP or HTTPS. Use this to get up-to-date documentation, library references, or API details from the web. Follows redirects automatically."""
    note_tool_call(deps, "webfetch", render_tool_details(url=url, max_chars=max_chars))
    return webfetch_tool(url, max_chars)


def _format_size(size: int) -> str:
    """Format byte size to human readable string."""
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    else:
        return f"{size / (1024 * 1024):.1f}MB"


def tool_history(deps: AgentDeps, n: int = 3) -> str:
    """View the last N command outputs from this workspace history."""
    # Handle n that might come as string from LLM
    try:
        n = int(n) if n is not None else 3
    except ValueError, TypeError:
        n = 3
    note_tool_call(deps, "history", render_tool_details(n=n))
    entries = load_history(deps.root)
    if not entries:
        return "<no history>"
    lines = []
    # Show last n entries, most recent first
    recent = entries[-n:] if n else entries
    for i, entry in enumerate(reversed(recent), 1):
        ts = entry.get("timestamp", "unknown")
        prompt = entry.get("prompt", "")
        output = entry.get("output", "")
        output_size = _format_size(len(output.encode("utf-8")))
        prompt_preview = prompt[:60] + "..." if len(prompt) > 60 else prompt
        lines.append(f"{i}. [{ts}] ({output_size}) {prompt_preview}")
        # Add output preview (first 2 lines, truncated)
        if output:
            output_lines = output.strip().splitlines()[:2]
            for ol in output_lines:
                ol_trunc = ol[:80] + "..." if len(ol) > 80 else ol
                lines.append(f"   {ol_trunc}")
    result = "\n".join(lines)
    print_preview(result, lines=1)
    return result


# Tool registry for dispatching tool calls
TOOLS = {
    "write": tool_write,
    "edit": tool_edit,
    "patch": tool_patch,
    "list": tool_list,
    "bash": tool_bash,
    "read": tool_read,
    "grep": tool_grep,
    "glob": tool_glob,
    "webfetch": tool_webfetch,
    "history": tool_history,
}


def _schema_from_func(name: str, func: callable) -> dict[str, Any]:
    """Generate an OpenAI function schema from a tool function's signature."""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    description = doc.split("\n\n")[0].strip() if doc else ""

    properties: dict[str, Any] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname == "deps":
            continue

        annotation = param.annotation
        if annotation is str:
            ptype = "string"
        elif annotation is int:
            ptype = "integer"
        elif annotation is bool:
            ptype = "boolean"
        elif annotation is float:
            ptype = "number"
        else:
            ptype = "string"

        properties[pname] = {"type": ptype}
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties},
        },
    }
    if required:
        schema["function"]["parameters"]["required"] = required
    return schema


# Generate TOOL_SCHEMAS from tool function signatures
TOOL_SCHEMAS = [_schema_from_func(name, func) for name, func in TOOLS.items()]


def list_model_ids() -> list[str]:
    require_api_env()
    with Spinner("listing models"):
        models = get_openai_client().models.list().data
    return sorted(model.id for model in models)


async def run_agent(
    prompt: str,
    model: str,
    root: Path,
    system_prompt: str,
    max_steps: int,
    max_tool_calls: int,
    save_to_history: bool = True,
    debug: bool = False,
) -> tuple[int, str]:
    """Run an agent using direct httpx calls to OpenAI API."""
    deps = AgentDeps(root=root, max_tool_calls=max_tool_calls)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    if debug:
        console.print("[bold yellow]DEBUG MODE ENABLED[/]")
        console.print(
            f"[dim]URL:[/dim] {os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')}/chat/completions"
        )
        console.print(f"[dim]Model:[/dim] {model}")
        console.print("[dim]System Prompt:[/dim]")
        console.print(
            Panel(system_prompt, title="System", border_style="dim", padding=(0, 1))
        )
        console.print("[dim]User Prompt:[/dim]")
        console.print(Panel(prompt, title="User", border_style="blue", padding=(0, 1)))
    else:
        console.print(f"[dim]→[/] {prompt[:100]}{'...' if len(prompt) > 100 else ''}")

    async def agent_loop(client: httpx.AsyncClient) -> tuple[int, str]:
        """Single agent loop - reused for retries."""
        step = 0
        while step < max_steps:
            step += 1

            request_body = {
                "model": model,
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
            }

            with Spinner("waiting for model"):
                response = await client.post("/chat/completions", json=request_body)
                response.raise_for_status()
                data = response.json()

            if debug:
                console.print("[dim]Request JSON:[/dim]")
                console.print_json(json.dumps(request_body))
                console.print("[dim]Response JSON:[/dim]")
                console.print_json(json.dumps(data))

                last_msg = request_body["messages"][-1]
                role = last_msg.get("role", "unknown")
                content = last_msg.get("content", "") or ""
                tool_calls = last_msg.get("tool_calls")
                console.print(
                    f"[dim]Request ({role}):[/dim] {content[:200]}{'...' if len(content) > 200 else ''}"
                )
                if tool_calls:
                    for tc in tool_calls:
                        if tc.get("type") == "function":
                            fn = tc["function"]
                            console.print(
                                f"[dim]  → {fn.get('name')}:[/dim] {str(fn.get('arguments', ''))[:100]}{'...' if len(str(fn.get('arguments', ''))) > 100 else ''}"
                            )

                choice_msg = data["choices"][0]["message"]
                resp_content = choice_msg.get("content", "") or ""
                resp_tool_calls = choice_msg.get("tool_calls")
                if resp_content:
                    console.print(
                        f"[dim]Response:[/dim] {resp_content[:200]}{'...' if len(resp_content) > 200 else ''}"
                    )
                if resp_tool_calls:
                    for tc in resp_tool_calls:
                        if tc.get("type") == "function":
                            fn = tc["function"]
                            console.print(
                                f"[dim]  → {fn.get('name')}:[/dim] {str(fn.get('arguments', ''))[:100]}{'...' if len(str(fn.get('arguments', ''))) > 100 else ''}"
                            )

            choice = data["choices"][0]
            message = choice["message"]

            if "tool_calls" in message and message["tool_calls"]:
                messages.append(message)

                for tool_call in message["tool_calls"]:
                    if tool_call["type"] != "function":
                        continue

                    function = tool_call["function"]
                    tool_name = function["name"]
                    args_str = function["arguments"]
                    tool_args = parse_tool_arguments(args_str)
                    function["arguments"] = json.dumps(tool_args)

                    if tool_name not in TOOLS:
                        result = f"Error: Unknown tool '{tool_name}'"
                    else:
                        try:
                            result = TOOLS[tool_name](deps, **tool_args)
                        except Exception as e:
                            result = f"Error: {e}"

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": str(result),
                        }
                    )
            else:
                output = message["content"] or ""
                render_agent_output(output)
                if save_to_history:
                    save_history(root, prompt, output)
                return 0, output

        return fail(f"reached max steps ({max_steps}) without a final response"), ""

    try:
        async with get_api_client() as client:
            return await agent_loop(client)
    except httpx.HTTPStatusError as exc:
        # Retry once on auth failure if using Bedrock (token may have expired)
        if exc.response.status_code in (401, 403) and refresh_bedrock_token():
            console.print("[dim]Bedrock token expired, refreshing...[/]")
            try:
                async with get_api_client() as client:
                    return await agent_loop(client)
            except httpx.HTTPStatusError as retry_exc:
                exc = retry_exc
            except Exception as retry_exc:
                return fail(str(retry_exc)), ""
        error_body = exc.response.text
        try:
            error_json = exc.response.json()
            error_body = json.dumps(error_json, indent=2)
        except Exception:
            pass
        return fail(f"API error {exc.response.status_code}: {error_body}"), ""
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc)), ""


@app.command("run")
def run_command(
    prompt: str | None = typer.Argument(
        None, help="Task for the assistant. Reads stdin if omitted."
    ),
    model: str | None = typer.Option(
        None, help="Model name. Defaults to saved config or moonshotai.kimi-k2.5."
    ),
    root: Path = typer.Option(Path("."), help="Workspace root."),
    system_file: Path | None = typer.Option(
        None,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Append extra system instructions from a file.",
    ),
    max_steps: int = typer.Option(DEFAULT_MAX_STEPS, min=1, hidden=True),
    max_tool_calls: int = typer.Option(DEFAULT_MAX_TOOL_CALLS, min=1, hidden=True),
    debug: bool = typer.Option(False, help="Print raw request/response for debugging."),
) -> None:
    require_runtime()
    task = prompt or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not task:
        abort("provide a prompt argument or pipe one on stdin")
    workspace = root.resolve()
    if not workspace.is_dir():
        abort(f"workspace root is not a directory: {workspace}")
    chosen_model = current_model(model)
    system_prompt = (
        SYSTEM_PROMPT
        if not system_file
        else SYSTEM_PROMPT + "\n\n" + system_file.read_text(encoding="utf-8")
    )
    print_event("root", str(workspace))
    print_event("model", chosen_model)
    exit_code, _ = asyncio.run(
        run_agent(
            task,
            chosen_model,
            workspace,
            system_prompt,
            max_steps,
            max_tool_calls,
            debug=debug,
        )
    )
    raise typer.Exit(exit_code)


@app.command("bedrock-token")
def bedrock_token(
    region: str | None = typer.Option(None, help="AWS region for Bedrock Mantle."),
) -> None:
    chosen = current_region(region)
    token = generate_bedrock_token(chosen)
    typer.echo(f"export OPENAI_BASE_URL={shlex.quote(bedrock_base_url(chosen))}")
    typer.echo(f"export OPENAI_API_KEY={shlex.quote(token)}")


@app.command("models")
def models_command() -> None:
    for model in list_model_ids():
        typer.echo(model)


@app.command("model")
def model_command(
    model: str | None = typer.Argument(None, help="Model id to save as the default."),
) -> None:
    if model is None:
        typer.echo(current_model(None))
        return
    save_config({**load_config(), "model": model})
    typer.echo(model)


def _split_args_and_prompt(args: list[str]) -> tuple[list[str], list[str]]:
    """Split args into options and prompt parts. Returns (options, prompt_parts)."""
    options: list[str] = []
    prompt_parts: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            # Everything after -- is prompt
            prompt_parts.extend(args[i + 1 :])
            break
        if arg.startswith("-"):
            options.append(arg)
            # Check if this option takes a value
            if arg in ("-m", "--model", "-r", "--root", "-s", "--system-file"):
                if i + 1 < len(args) and not args[i + 1].startswith("-"):
                    options.append(args[i + 1])
                    i += 1
            i += 1
        else:
            # First non-option and everything after is prompt
            prompt_parts.extend(args[i:])
            break
    return options, prompt_parts


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "bedrock-token", "models", "model", "-h", "--help"}
    if not args:
        args = ["run"] if not sys.stdin.isatty() else ["--help"]
    elif args[0] not in commands and args[0].startswith("-"):
        args = ["run", *args]
    elif args[0] not in commands:
        args = ["run", *args]

    # Join multiple args into a single prompt for run command
    if args and args[0] == "run" and len(args) > 2:
        options, prompt_parts = _split_args_and_prompt(args[1:])
        if len(prompt_parts) > 1:
            args = ["run"] + options + [" ".join(prompt_parts)]

    try:
        app(args=args, standalone_mode=False)
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
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
from openai import (
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)  # pyright: ignore[reportMissingImports]

from rich.console import Console
from rich.json import JSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.text import Text

__version__ = "0.1.2"

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


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-v", help="Show version and exit."
    ),
) -> None:
    """oy - tiny local coding assistant."""
    if version:
        typer.echo(f"oy {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


console = Console(stderr=True, highlight=False, soft_wrap=True)
output_console = Console(highlight=False, soft_wrap=True)


class Spinner:
    def __init__(self, label: str) -> None:
        self.label = label
        self.status: Status | None = None

    def __enter__(self) -> None:
        if not sys.stderr.isatty():
            console.print(f"[bright_black]>[/] {self.label}")
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
    """Render tool call details as compact output."""
    parts = []
    for key, value in details.items():
        if value is None or value == "":
            continue
        # For booleans, just show key if true
        if isinstance(value, bool):
            if value:
                parts.append(key)
        else:
            preview = render_preview(value, limit=50)
            parts.append(f"{key}={preview}")
    return " ".join(parts)


def parse_tool_arguments(args_str: str) -> dict[str, Any]:
    """Parse tool arguments from LLM output.

    Some LLMs occasionally emit duplicated JSON arguments (e.g., the same object twice).
    This workaround hunts for valid JSON around the midpoint of malformed responses.
    """

    def decode(candidate: str) -> dict[str, Any]:
        parsed = json.loads(candidate)
        parsed = json.loads(parsed) if isinstance(parsed, str) else parsed
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object")
        return parsed

    try:
        return decode(args_str)
    except (json.JSONDecodeError, ValueError) as exc:
        # Workaround for LLMs that duplicate the JSON in tool call arguments
        mid = len(args_str) // 2

        # Hunt for '{' in a window around the midpoint
        for i in range(max(0, mid - 15), min(len(args_str), mid + 15)):
            if args_str[i] == "{":
                try:
                    return decode(args_str[i:])
                except json.JSONDecodeError, ValueError:
                    pass

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
            console.print(f"> {label}")
            console.print(json.dumps(value, indent=2, ensure_ascii=True))
    else:
        text = f"[bold cyan]>[/] {label}"
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
    """Print short preview of output using terminal width."""
    if not text:
        return
    width = min(console.width - 6, 120)  # Leave room for prefix, cap at 120
    preview_lines = text.splitlines()[:lines]
    for line in preview_lines:
        truncated = line[:width] if len(line) > width else line
        console.print(f"  [bright_black]│[/] [dim]{truncated}[/]")


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


class HistoryStore:
    """Simple file history storage using diskcache."""

    def __init__(self, cache_path: Path = HISTORY_CACHE_PATH) -> None:
        self._cache_path = cache_path
        cache_path.mkdir(parents=True, exist_ok=True)
        self._cache = diskcache.Cache(str(cache_path))

    def _key(self, root: Path) -> str:
        return str(root.resolve())

    def load(self, root: Path) -> list[dict[str, Any]]:
        return self._cache.get(self._key(root), [])

    def save(self, root: Path, prompt: str, output: str) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt": prompt,
            "output": output,
        }
        key = self._key(root)
        history = self._cache.get(key, [])
        history.append(entry)
        self._cache[key] = history[-MAX_HISTORY_PER_DIR:]


_history_store: HistoryStore | None = None


def get_history_store() -> HistoryStore:
    global _history_store
    if _history_store is None:
        _history_store = HistoryStore()
    return _history_store


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
    """Resolve a path relative to root, preventing directory traversal.

    If the resolved path would escape root, returns root/basename instead.
    This is a security measure to prevent reading/writing outside the workspace.
    """
    path = (root / raw).resolve()
    if path != root and root not in path.parents:
        return root / Path(raw).name
    return path


def tool_list(deps: AgentDeps, path: str = ".", limit: int = 200) -> str:
    """List files and directories in a workspace directory. Use this instead of `bash` with `ls`."""
    note_tool_call(deps, "list", render_tool_details(path=path, limit=limit))
    target = resolve_path(deps.root, path)
    if not target.is_dir():
        raise ValueError("path is not a directory")
    items = [
        rel(deps.root, item) + ("/" if item.is_dir() else "")
        for item in sorted(target.iterdir())[: max(limit, 1)]
    ]
    output = clip("\n".join(items) or "<empty directory>")
    print_preview(output, lines=1)
    return output


def tool_read(deps: AgentDeps, path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a file in the workspace. Use this instead of `cat`, `head`, or `tail`."""
    note_tool_call(
        deps, "read", render_tool_details(path=path, offset=offset, limit=limit)
    )
    target = resolve_path(deps.root, path)
    if target.is_dir():
        return tool_list(deps, path, limit)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(offset, 1) - 1
    body = [
        f"{i + 1}: {line}"
        for i, line in enumerate(lines[start : start + max(limit, 1)], start=start)
    ]
    return clip("\n".join(body) or "<empty file>")


def tool_write(deps: AgentDeps, path: str, content: str) -> str:
    """Create or overwrite a file in the workspace."""
    note_tool_call(deps, "write", render_tool_details(path=path, chars=len(content)))
    target = resolve_path(deps.root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    result = f"wrote {rel(deps.root, target)} ({len(content)} chars)"
    print_preview(result, lines=1)
    return result


def tool_edit(
    deps: AgentDeps,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
) -> str:
    """Replace text in an existing workspace file."""
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
    if not old_text:
        raise ValueError("old_text must not be empty")
    target = resolve_path(deps.root, path)
    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_text)
    if count == 0:
        raise ValueError("old_text not found")
    if count > 1 and not replace_all:
        raise ValueError("old_text matched multiple locations; set replace_all=true")

    new_content = (
        text.replace(old_text, new_text)
        if replace_all
        else text.replace(old_text, new_text, 1)
    )
    target.write_text(new_content, encoding="utf-8")
    result = f"edited {rel(deps.root, target)} ({count} replacement{'s' if count != 1 else ''})"
    print_preview(result, lines=1)
    return result


def tool_patch(deps: AgentDeps, patch_text: str) -> str:
    """Apply a unified diff inside the workspace."""
    note_tool_call(deps, "patch", render_tool_details(chars=len(patch_text)))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(patch_text)
        patch_file = handle.name
    try:
        result = run(
            [
                "patch",
                "--strip=0",
                "--directory",
                str(deps.root),
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


def tool_bash(deps: AgentDeps, command: str, timeout_seconds: int = 120) -> str:
    """Last resort: run shell commands for builds, tests, git, package managers, or other real terminal tasks."""
    note_tool_call(
        deps,
        "bash",
        render_tool_details(command=command, timeout=timeout_seconds),
    )
    result = run(["bash", "-lc", command], cwd=deps.root, timeout=timeout_seconds)
    # Show exit code prominently
    style = "green" if result.returncode == 0 else "red"
    console.print(f"  [dim]exit[/] [{style}]{result.returncode}[/]")
    # Show output preview (stdout + stderr)
    output_parts = []
    if result.stdout.strip():
        output_parts.append(result.stdout.strip())
    if result.stderr.strip():
        output_parts.append(result.stderr.strip())
    output_text = "\n".join(output_parts)
    if output_text:
        print_preview(output_text, lines=3)
    output = f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
    return clip(output)


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
    command.extend([pattern, str(resolve_path(deps.root, path))])
    result = run(command)
    if result.returncode not in (0, 1):
        raise ValueError(result.stderr.strip() or "rg failed")
    output = result.stdout.strip() or "<no matches>"
    print_preview(output, lines=3)
    return clip(output)


def tool_glob(deps: AgentDeps, pattern: str, path: str = ".") -> str:
    """Find files or directories with glob patterns. Use this instead of `find` in bash."""
    note_tool_call(deps, "glob", render_tool_details(pattern=pattern, path=path))
    base = resolve_path(deps.root, path)
    items = [
        rel(deps.root, match) + ("/" if match.is_dir() else "")
        for match in sorted(base.glob(pattern))[:200]
    ]
    output = "\n".join(items) or "<no matches>"
    print_preview(output, lines=1)
    return clip(output)


def tool_webfetch(deps: AgentDeps, url: str, max_chars: int = MAX_CHARS) -> str:
    """Fetch a web page over HTTP or HTTPS. Use this to get up-to-date documentation, library references, or API details from the web. Follows redirects automatically."""
    note_tool_call(deps, "webfetch", render_tool_details(url=url, max_chars=max_chars))
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
        # Special handling for bash commands - show command prominently
        cmd = details[4:]  # Remove "cmd=" prefix
        console.print(f"[bright_black]>[/] [bold]{name}[/]", highlight=False)
        if sys.stderr.isatty() and cmd:
            # Use simple text with bash highlighting color
            text = Text(cmd, style="cyan")
            console.print(text)
    elif details:
        console.print(
            f"[bright_black]>[/] [bold]{name}[/] [dim]{details}[/]", highlight=False
        )
    else:
        console.print(f"[bright_black]>[/] [bold]{name}[/]", highlight=False)


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
    note_tool_call(deps, "history", render_tool_details(n=n))
    entries = get_history_store().load(deps.root)
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


def tool_schema(func: Any) -> dict[str, Any]:
    """Generate OpenAI function schema from a tool function's signature."""
    import inspect

    sig = inspect.signature(func)
    params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    type_map = {str: "string", int: "integer", bool: "boolean"}

    for name, param in sig.parameters.items():
        if name == "deps":
            continue
        ptype = type_map.get(param.annotation, "string")
        params["properties"][name] = {"type": ptype}
        if param.default is inspect.Parameter.empty:
            params["required"].append(name)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": (func.__doc__ or "").split("\n")[0],
            "parameters": params,
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [tool_schema(func) for func in TOOLS.values()]


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

    def debug_startup() -> None:
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

    def debug_request_response(
        request_body: dict[str, Any], data: dict[str, Any]
    ) -> None:
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

    if debug:
        debug_startup()
    else:
        console.print(f"[dim]→[/] {prompt[:100]}{'...' if len(prompt) > 100 else ''}")

    async def agent_loop(client: AsyncOpenAI) -> tuple[int, str]:
        """Single agent loop - reused for retries."""
        step = 0
        while step < max_steps:
            step += 1

            with Spinner("waiting for model"):
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    tools=TOOL_SCHEMAS,  # type: ignore[arg-type]
                    tool_choice="auto",
                )

            # Convert SDK response to mutable dict
            message = response.choices[0].message.model_dump(exclude_none=True)
            if debug:
                debug_request_response(
                    {
                        "model": model,
                        "messages": messages,
                        "tools": TOOL_SCHEMAS,
                        "tool_choice": "auto",
                    },
                    {"choices": [{"message": message}]},
                )

            if "tool_calls" in message and message["tool_calls"]:
                # Sanitize tool arguments before appending to prevent context pollution
                for tool_call in message["tool_calls"]:
                    if tool_call.get("type") != "function":
                        continue

                    function = tool_call["function"]
                    tool_name = function["name"]
                    args_str = function["arguments"]
                    tool_args = parse_tool_arguments(args_str)
                    function["arguments"] = json.dumps(tool_args)

                # Now append the sanitized message
                messages.append(message)

                # Execute tools
                for tool_call in message["tool_calls"]:
                    if tool_call.get("type") != "function":
                        continue

                    function = tool_call["function"]
                    tool_name = function["name"]
                    tool_args = parse_tool_arguments(function["arguments"])

                    # Strip 'tool_' prefix if present for lookup
                    lookup_name = (
                        tool_name[5:] if tool_name.startswith("tool_") else tool_name
                    )
                    if lookup_name not in TOOLS:
                        result = f"Error: Unknown tool '{tool_name}'"
                    else:
                        try:
                            result = TOOLS[lookup_name](deps, **tool_args)
                        except Exception as e:
                            import traceback

                            tb_lines = traceback.format_exc().splitlines()
                            # Show last 3 lines of traceback for context
                            tb_preview = (
                                "\n".join(tb_lines[-3:])
                                if len(tb_lines) > 3
                                else "\n".join(tb_lines)
                            )
                            result = f"Error in {tool_name}: {type(e).__name__}: {e}\n{tb_preview}"

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
                    get_history_store().save(root, prompt, output)
                return 0, output

        return fail(f"reached max steps ({max_steps}) without a final response"), ""

    try:
        client = get_openai_client(async_=True)
        return await agent_loop(client)
    except AuthenticationError:
        # Retry once on auth failure if using Bedrock (token may have expired)
        if refresh_bedrock_token():
            console.print("[dim]Bedrock token expired, refreshing...[/]")
            try:
                client = get_openai_client(async_=True)
                return await agent_loop(client)
            except (AuthenticationError, PermissionDeniedError) as retry_exc:
                exc = retry_exc  # type: ignore[assignment]
            except Exception as retry_exc:
                return fail(str(retry_exc)), ""
        return fail(f"API authentication error: {exc}"), ""
    except PermissionDeniedError:
        if refresh_bedrock_token():
            console.print("[dim]Bedrock token expired, refreshing...[/]")
            try:
                client = get_openai_client(async_=True)
                return await agent_loop(client)
            except (AuthenticationError, PermissionDeniedError) as retry_exc:
                exc = retry_exc  # type: ignore[assignment]
            except Exception as retry_exc:
                return fail(str(retry_exc)), ""
        return fail(f"API permission denied: {exc}"), ""
    except RateLimitError as exc:
        return fail(f"API rate limit: {exc}"), ""
    except BadRequestError as exc:
        return fail(f"API bad request: {exc}"), ""
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc)), ""


@app.command("run")
def run_command(
    prompt: list[str] = typer.Argument(
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
    task = (
        " ".join(prompt)
        if prompt
        else (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    )
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


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {
        "run",
        "bedrock-token",
        "models",
        "model",
        "-h",
        "--help",
        "-v",
        "--version",
    }
    if not args:
        args = ["run"] if not sys.stdin.isatty() else ["--help"]
    elif args[0] not in commands:
        args = ["run", *args]

    try:
        app(args=args, standalone_mode=False)
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    main()

from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
import defopt
import httpx
from markdownify import markdownify as html_to_markdown
from openai import AsyncOpenAI, OpenAI
from openai import (
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from rich.console import Console
from rich import filesize
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.status import Status

__version__ = "0.2.1"
# Per-tool payloads should stay comfortable for long sessions on 128k-ish models.
MAX_TOOL_OUTPUT_CHARS = 16000
MAX_TOOL_OUTPUT_TAIL_CHARS = 4000
MAX_HTTPX_CHARS = 20000
DEFAULT_MODEL = "moonshotai.kimi-k2.5"
DEFAULT_REGION = (
    os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
)
DEFAULT_MAX_STEPS = 512
DEFAULT_MAX_TOOL_CALLS = 512
CONFIG_PATH = Path.home() / ".config" / "oy" / "config.json"
BASE_SYSTEM_PROMPT = """You are oy, a tiny local coding assistant.

## Core Principles

- Work simply, inspect before changing, prefer secure boring solutions.
- Each run is a fresh session: no hidden memory or prior conversation state.
- If context is missing, ask the user to restate or paste it—don't guess.
- Keep answers concise but thorough—stay on task until finished or blocked.

## Tool Selection Guide

| Need | Tool | Notes |
|------|------|-------|
| Read a file | read | Always read first. Use offset/limit for large files. |
| List directory | list | Discover structure before diving deep. |
| Find by pattern | glob | Know the filename pattern but not the path? Use this. |
| Search contents | grep | Know the text/regex but not the file? Use this. |
| Edit files | apply | Replace exact strings, write new files, move, or delete. Batch related edits. |
| Run commands | bash | For builds, tests, git, package managers. Not for reading files. |
| Fetch web/API | httpx | Fetch documentation, standards, API responses. Presets: 'page' (HTML→markdown), 'json', 'post_json'. |

## Output Truncation

Tool output clips to preserve context (~16k chars, ~20k for httpx). Bash preserves both head AND tail.

When clipped: use read with offsets, grep, glob, list, or follow-up httpx calls—never guess.

## File Editing Workflow

1. Read the file first.
2. Use apply with exact string replacement.
3. For new files, use apply with op='write'.
4. Batch related operations.
"""
INTERACTIVE_SYSTEM_PROMPT = """## Interactive Mode

This run is interactive—the ask tool is available for human collaboration.

### When to Use ask

Use ask for:
- **Plans**: When the user requests a plan, inspect code, draft a short plan, then ask if it looks good.
- **Ambiguity**: When multiple reasonable approaches exist and user preference matters.
- **Checkpoints**: For multi-batch work, offer checkpoints between batches.
- **Risky changes**: Before broad refactors or many coordinated edits, ask if the user wants a summary and commit first (for easy undo).
- **Completion**: After making changes, ask "Would you like me to summarise and commit the work completed?"

### How to Use ask Efficiently

- Don't ask after every trivial step—batch related work, then checkpoint.
- For multi-step tasks, work in 2-3 meaningful batches with ask between them.
- Present clear options when possible: ask("Which approach?", choices=["option A", "option B"])
- If the user gives feedback, revise and continue.

### Forced Checkpoints

Always ask before:
- Deleting files (confirm with ask)
- Making irreversible changes without backups
- Starting a task that will modify many files
"""
NONINTERACTIVE_SYSTEM_PROMPT = """## Non-Interactive Mode

This run is non-interactive—the ask tool is **unavailable**. Do not pause for approvals or checkpoints.

### Autonomy Guidelines

- Complete the prompted work without interruptions.
- Use workspace inspection (read, grep, glob, list) to resolve uncertainty.
- Choose reasonable, safe defaults when context provides direction.
- Keep going unless blocked by: missing credentials, an irreversible decision, or unresolvable ambiguity.

### Error Recovery

Be resilient to recoverable faults:
- Command fails? → Check error message, adjust command, retry.
- File not found? → Use grep/glob to locate it.
- Output truncated? → Narrow the query (read with offset, more specific grep).
- Tool unsuitable? → Switch to a better tool for the job.
- Complex approach failing? → Take a simpler path.

### Blocking Conditions

Only stop when truly blocked. If blocked, provide a concise status:
1. What you tried
2. What remains blocked
3. The smallest useful next step for the operator

Never stop at the first failure—always attempt recovery first.
"""
AUDIT_SYSTEM_PROMPT = """## Security and Complexity Audit

You are conducting a security and code quality audit of this repository.

### Process

1. Fetch current OWASP ASVS/MSVS standards via httpx:
   - ASVS: https://raw.githubusercontent.com/OWASP/ASVS/master/README.md
   - MSVS: https://raw.githubusercontent.com/OWASP/owasp-masvs/master/README.md
2. Explore the repository structure systematically
3. Read and analyze all source code files
4. Identify security vulnerabilities and code complexity issues
5. Prioritize issues by severity (critical/high/medium/low)

### Output: ISSUES.md

Use the apply tool to write findings directly to ISSUES.md in this format:

```markdown
# Audit Findings

> Last audit: [timestamp]

## Summary

Total issues found: [count]

## Critical Severity (N)

### 1. [Title]

- **Location**: `file:line`
- **Category**: security|complexity
- **Standard**: ASVS V5: Security Configuration

[Description]

**Recommendation**: [Action to fix]

**Status**: OPEN

---

## High Severity (N)
...
```

Include a `**Status**: OPEN` line for each finding so humans can track progress.
Append to any existing content in ISSUES.md; do not delete human-added notes.
"""
BREW_CANDIDATES = [
    Path("/home/linuxbrew/.linuxbrew/bin/brew"),
    Path("/opt/homebrew/bin/brew"),
    Path("/usr/local/bin/brew"),
]
MISE_CANDIDATES = [
    Path.home() / ".local" / "bin" / "mise",
    Path.home() / ".cargo" / "bin" / "mise",
    Path("/usr/local/bin/mise"),
    Path("/opt/homebrew/bin/mise"),
    Path("/home/linuxbrew/.linuxbrew/bin/mise"),
]
SSO_MARKERS = (
    "error loading sso token",
    "the sso session associated with this profile has expired",
    "the sso session has expired or is otherwise invalid",
    "to refresh this sso session run aws sso login",
)
SEARCH_BACKENDS = {
    "rg": lambda exe, pattern, path, glob: [
        exe,
        "--line-number",
        "--column",
        "--color",
        "never",
        "--hidden",
        "--glob",
        "!.git",
        *(["--glob", glob] if glob else []),
        pattern,
        path,
    ],
    "grep": lambda exe, pattern, path, glob: [
        exe,
        "-rnE",
        "--exclude-dir=.git",
        *(["--include", glob] if glob else []),
        pattern,
        path,
    ],
}
STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
STRINGS = {"type": "array", "items": STR}
APPLY_OPERATION = {
    "type": "object",
    "properties": {
        "op": {"type": "string", "enum": ["replace", "write", "move", "delete"]},
        "path": STR,
        "old": STR,
        "new": STR,
        "replace_all": BOOL,
        "content": STR,
        "overwrite": BOOL,
        "to": STR,
    },
    "required": ["op", "path"],
}
APPLY_OPERATIONS = {"type": "array", "items": APPLY_OPERATION}
_using_bedrock = False
_last_api_env_error = None
STDOUT = Console()
STDERR = Console(stderr=True)
# Fit terminal display nicely; Rich handles wrapping automatically
DISPLAY_MAX_WIDTH = 120  # Reasonable max width for tool output previews
HTML_MARKERS = ("text/html", "application/xhtml+xml")
HTTPX_PRESET = {"type": "string", "enum": ["page", "json", "post_json"]}
HTTPX_RESPONSE_MODE = {"type": "string", "enum": ["auto", "headers", "body", "json"]}
MAP = {"type": "object"}
ANY_JSON = {}


def markdown(text="", *, stderr=False):
    console = STDERR if stderr else STDOUT
    if text:
        console.print(Markdown(str(text)))
    else:
        console.print()


def code_block(text, language="text"):
    body = str(text).rstrip("\n")
    return f"```{language}\n{body}\n```"


def format_bash_result(command, returncode, stdout, stderr):
    """Format bash command output as a pretty markdown block."""
    parts = ["```bash", f"$ {command}"]
    stdout = (stdout or "").rstrip()
    stderr = (stderr or "").rstrip()
    if stdout:
        parts.append(stdout)
    if returncode != 0:
        parts.append(f"# exit {returncode}")
    if stderr:
        parts.extend(["# stderr:", stderr])
    parts.append("```")
    return "\n".join(parts)


def inline_code(text):
    value = str(text).replace("`", "\\`")
    return f"`{value}`"


def status(text=""):
    if text:
        markdown(f"- {text}", stderr=True)


def warning(text=""):
    markdown(f"- **Warning:** {text}", stderr=True)


def error(text=""):
    message = str(text).strip()
    body = message if "\n" in message else f"- {message}"
    markdown(f"## Error\n\n{body}", stderr=True)


def prompt_text(text=""):
    markdown(f"### {text}", stderr=True)


def fail(message, code=1):
    error(message)
    return code


def abort(message, code=1):
    raise SystemExit(fail(message, code))


def clip(text, limit=MAX_TOOL_OUTPUT_CHARS, tail_chars=0):
    """Truncate text to a character limit, optionally preserving tail.

    For bash output, use tail_chars > 0 to show both head and tail with a marker.
    Otherwise, just truncates at limit with an omission count.
    """
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    if 0 < tail_chars < limit:
        head_chars = max(limit - tail_chars, 1)
        marker = f"\n... [{omitted} chars omitted; showing first {head_chars} and last {tail_chars}]\n"
        head = text[:head_chars]
        tail = text[-tail_chars:]
        return head + marker + tail
    return f"{text[:limit]}\n... [{omitted} chars omitted after {limit}]"


def preview(value, limit=72):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def compact_markdown(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def should_markdownify_html(content_type, text):
    """Determine if HTTP response body should be converted from HTML to markdown.

    Checks content-type header and probes the first 500 chars for HTML markers.
    """
    lowered = (content_type or "").lower()
    if any(marker in lowered for marker in HTML_MARKERS):
        return True
    probe = text.lstrip()[:500].lower()
    return (
        probe.startswith("<!doctype html")
        or probe.startswith("<html")
        or ("<body" in probe and "<p" in probe)
    )


def format_http_text_body(text, content_type):
    if not should_markdownify_html(content_type, text):
        return text
    converted = compact_markdown(
        html_to_markdown(
            text,
            heading_style="ATX",
            bullets="-",
            strip=["script", "style", "noscript", "svg", "canvas"],
        )
    )
    return converted or text


def parse_json_path(path):
    return [part for part in (path or "").split(".") if part]


def select_json_path(value, path):
    current = value
    for part in parse_json_path(path):
        if isinstance(current, list):
            if not part.isdigit():
                raise ValueError(
                    f"json_path expected list index, got {inline_code(part)}"
                )
            index = int(part)
            try:
                current = current[index]
            except IndexError as exc:
                raise ValueError(f"json_path index out of range: {index}") from exc
            continue
        if isinstance(current, dict):
            if part not in current:
                raise ValueError(f"json_path key not found: {inline_code(part)}")
            current = current[part]
            continue
        raise ValueError(f"json_path cannot descend into {type(current).__name__}")
    return current


def normalize_mapping(value, name):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): "" if item is None else str(item) for key, item in value.items()}


def redact_header_value(name, value):
    lowered = name.lower()
    if lowered in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
        return "<redacted>"
    if any(marker in lowered for marker in ("token", "secret", "api-key", "apikey")):
        return "<redacted>"
    return value


def render_response_headers(headers):
    return "\n".join(
        f"{name}: {redact_header_value(name, value)}" for name, value in headers.items()
    )


def httpx_error_message(exc, timeout_seconds):
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if isinstance(exc, httpx.TimeoutException):
        return f"request timed out after {timeout_seconds} seconds"
    if "certificate verify failed" in lowered or "tls" in lowered:
        return "TLS verification failed; check the certificate chain or use a trusted HTTPS endpoint"
    if isinstance(exc, httpx.NetworkError):
        return f"network error: {message}"
    return f"request failed: {message}"


def render_httpx_output(response, response_mode, json_path=None):
    content_type = response.headers.get("content-type", "")
    lines = [
        f"url: {response.url}",
        f"status: {response.status_code}",
        f"reason: {response.reason_phrase}",
        f"content-type: {content_type or '<unknown>'}",
    ]
    mode = response_mode
    if mode == "auto":
        ct_lowered = (content_type or "").lower()
        is_json = "application/json" in ct_lowered or "+json" in ct_lowered
        mode = "json" if json_path or is_json else "body"
    if mode == "headers":
        header_block = render_response_headers(response.headers)
        lines.append("headers:")
        lines.append(header_block or "<none>")
        return "\n".join(lines)
    if mode == "json":
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError("response body is not valid JSON") from exc
        else:
            if json_path:
                payload = select_json_path(payload, json_path)
                lines.append(f"json-path: {json_path}")
            lines.append("body-format: json")
            lines.append("")
            body = (
                payload
                if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=True, indent=2)
            )
            lines.append(body)
            return "\n".join(lines)
    body = format_http_text_body(response.text, content_type)
    if body != response.text:
        lines.append("body-format: markdown")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)


def show(text, lines=2):
    """Display a preview of tool output with intelligent truncation.

    Renders as Markdown for proper code block display.
    Shows first N lines (default 2) with truncation indicator if needed.
    For code blocks, detects and preserves proper fencing.
    """
    if not text:
        return

    lines_to_show = max(lines, 0)
    text_lines = text.splitlines()

    if len(text_lines) <= lines_to_show:
        # Output fits, render as markdown
        STDERR.print(Markdown(text), width=DISPLAY_MAX_WIDTH, overflow="fold")
        return

    # Need to truncate: show first N lines
    snippet = "\n".join(text_lines[:lines_to_show])
    total_lines = len(text_lines)
    omitted_lines = total_lines - lines_to_show

    # Check if we've created an unclosed code block by truncation
    # Count fences in snippet vs full text
    snippet_fences = snippet.count("```")
    full_fences = text.count("```")
    needs_close = snippet_fences % 2 == 1 and full_fences % 2 == 0

    # Build output as markdown with truncation marker
    parts = [snippet]
    if omitted_lines > 0:
        msg = "line" if omitted_lines == 1 else "lines"
        parts.append(f"\n... [{omitted_lines} more {msg}]")
    if needs_close:
        parts.append("\n```")

    STDERR.print(Markdown("\n".join(parts)), width=DISPLAY_MAX_WIDTH, overflow="fold")


def render_markdown(text):
    markdown(text)


def rel(root, path):
    try:
        return path.relative_to(root).as_posix() or "."
    except ValueError:
        return path.as_posix()


def split_path(value):
    return [entry for entry in (value or "").split(os.pathsep) if entry]


def merge_paths(*groups):
    merged, seen = [], set()
    for group in groups:
        for entry in group:
            key = os.path.normcase(os.path.normpath(entry))
            if entry and key not in seen:
                seen.add(key)
                merged.append(entry)
    return os.pathsep.join(merged)


def which(tool, path_value=None, candidates=None):
    if found := shutil.which(tool, path=path_value):
        return found
    for candidate in candidates or []:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run_cmd(command, cwd=None, env=None, timeout=120):
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=max(timeout, 1),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"command timed out after {timeout} seconds") from exc


def command_env(cwd=None):
    """Build environment for shell commands, incorporating mise and homebrew paths.

    Merges PATH entries from mise env --json and homebrew prefix detection.
    This ensures tools installed via mise or homebrew are available.
    """
    env, brew_bins = os.environ.copy(), BREW_CANDIDATES.copy()
    if prefix := env.get("HOMEBREW_PREFIX") or os.environ.get("HOMEBREW_PREFIX"):
        brew_bins.insert(0, Path(prefix) / "bin" / "brew")
    if brew := which("brew", env.get("PATH"), brew_bins):
        prefix = Path(brew).parent.parent
        env["HOMEBREW_PREFIX"] = str(prefix)
        env["PATH"] = merge_paths(
            [str(prefix / "bin"), str(prefix / "sbin")], split_path(env.get("PATH"))
        )
    if not (mise := which("mise", env.get("PATH"), MISE_CANDIDATES)):
        return env
    try:
        result = run_cmd(
            [mise, "env", "--json"],
            cwd=cwd if cwd and cwd.is_dir() else None,
            env=env,
            timeout=5,
        )
        data = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return env
    if not isinstance(data, dict):
        return env
    merged = env.copy()
    for key, value in data.items():
        if isinstance(value, str):
            merged[key] = (
                merge_paths(split_path(value), split_path(env.get("PATH")))
                if key == "PATH"
                else value
            )
    return merged


def aws_cli(parts, cwd=None, timeout=10):
    env = command_env(cwd)
    if not (aws := which("aws", env.get("PATH"))):
        raise RuntimeError("AWS CLI is not installed or not on PATH")
    return run_cmd([aws, *parts], cwd=cwd, env=env, timeout=timeout)


def run_aws_sso_login(cwd=None):
    env = command_env(cwd)
    if not (aws := which("aws", env.get("PATH"))):
        raise RuntimeError("AWS CLI is not installed or not on PATH")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError(
            "AWS SSO session is stale. Run `aws sso login --use-device-code --no-browser` and retry."
        )
    profile = env.get("AWS_PROFILE")
    status(
        "Refreshing AWS SSO session"
        + (f" for profile {inline_code(profile)}" if profile else "")
        + " with device-code login."
    )
    result = run_cmd(
        [
            aws,
            "sso",
            "login",
            "--use-device-code",
            "--no-browser",
            "--no-cli-pager",
        ],
        cwd=cwd,
        env=env,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(f"AWS SSO login failed with exit code {result.returncode}")


def load_aws_credentials(cwd=None, allow_login=True):
    """Load AWS credentials via aws configure export-credentials.

    If SSO session is stale and allow_login is True, attempts automatic refresh
    via `aws sso login --use-device-code --no-browser`.

    Returns dict with access_key, secret_key, and optionally session_token.
    """
    result = aws_cli(
        ["configure", "export-credentials", "--format", "process", "--no-cli-pager"],
        cwd=cwd,
        timeout=30,
    )
    if result.returncode:
        message = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"AWS CLI exited with status {result.returncode}"
        )
        stale = any(marker in message.lower() for marker in SSO_MARKERS) or (
            "token for" in message.lower() and "does not exist" in message.lower()
        )
        if allow_login and stale:
            run_aws_sso_login(cwd)
            return load_aws_credentials(cwd, False)
        raise RuntimeError(message)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI returned invalid credential JSON") from exc
    access_key, secret_key, session_token = (
        payload.get("AccessKeyId"),
        payload.get("SecretAccessKey"),
        payload.get("SessionToken"),
    )
    if not isinstance(access_key, str) or not isinstance(secret_key, str):
        raise RuntimeError("AWS CLI did not return usable Bedrock credentials")
    creds = {"access_key": access_key, "secret_key": secret_key}
    if isinstance(session_token, str) and session_token:
        creds["session_token"] = session_token
    return creds


def sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key, date_stamp, region, service):
    key = sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    for part in (region, service, "aws4_request"):
        key = sign(key, part)
    return key


def make_bedrock_token(region, cwd=None, expires=43200):
    """Generate a Bedrock bearer token using AWS SigV4 signing.

    Takes AWS credentials and produces a token suitable for the Bedrock API.
    Token is valid for `expires` seconds (default 12 hours).
    """
    creds = load_aws_credentials(cwd)
    now = datetime.now(timezone.utc)
    amz_date, date_stamp = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    query = [
        ("Action", "CallWithBearerToken"),
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
        (
            "X-Amz-Credential",
            f"{creds['access_key']}/{date_stamp}/{region}/bedrock/aws4_request",
        ),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    if token := creds.get("session_token"):
        query.append(("X-Amz-Security-Token", token))
    canonical = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query)
    )
    request = f"POST\n/\n{canonical}\nhost:bedrock.amazonaws.com\n\nhost\n{hashlib.sha256(b'').hexdigest()}"
    scope = f"{date_stamp}/{region}/bedrock/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(request.encode()).hexdigest()}"
    signature = hmac.new(
        signing_key(creds["secret_key"], date_stamp, region, "bedrock"),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    raw = f"bedrock.amazonaws.com/?{canonical}&X-Amz-Signature={signature}&Version=1"
    return f"bedrock-api-key-{base64.b64encode(raw.encode()).decode()}"


def provide_token(region=None, cwd=None):
    if not (
        region := region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    ):
        raise RuntimeError(
            "Region must be provided or set via AWS_REGION/AWS_DEFAULT_REGION"
        )
    return make_bedrock_token(region, cwd)


def config_path():
    return Path(os.environ.get("OY_CONFIG", str(CONFIG_PATH))).expanduser()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_config():
    data = load_json(config_path(), {})
    return data if isinstance(data, dict) else {}


def save_config(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def setting(choice, env_keys, config_key, default):
    if choice:
        return choice
    for key in env_keys:
        if value := os.environ.get(key):
            return value
    return load_config().get(config_key, default) if config_key else default


def find_model_by_suffix(models, suffix):
    """Return first model ID ending with suffix, or None."""
    for m in models:
        if m.endswith(suffix):
            return m
    return None


def pick_default_model():
    """Try glm-5 then kimi-k2.5 via suffix match on available models."""
    try:
        available = list_model_ids()
    except Exception:
        return DEFAULT_MODEL
    for suffix in ("glm-5", "kimi-k2.5"):
        if m := find_model_by_suffix(available, suffix):
            return m
    return DEFAULT_MODEL


def current_model(choice):
    if choice:
        return choice
    if os.environ.get("OY_MODEL"):
        return os.environ["OY_MODEL"]
    if model := load_config().get("model"):
        return model
    return pick_default_model()


def current_region(choice):
    return setting(choice, ("AWS_REGION", "AWS_DEFAULT_REGION"), None, DEFAULT_REGION)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    abort(
        f"Invalid value for {inline_code(name)}: {inline_code(value)}. Use 1/0, true/false, yes/no, or on/off."
    )
    return default


def current_workspace() -> Path:
    return Path(os.environ.get("OY_ROOT", ".")).expanduser()


def current_system_file() -> Path | None:
    raw = os.environ.get("OY_SYSTEM_FILE")
    return Path(raw).expanduser() if raw else None


def current_non_interactive() -> bool:
    return env_flag("OY_NON_INTERACTIVE", False)


def bedrock_base_url(region):
    return f"https://bedrock-mantle.{region}.api.aws/v1"


def ensure_api_env(cwd=None, refresh=False):
    global _using_bedrock, _last_api_env_error
    if refresh and not _using_bedrock:
        return False
    if os.environ.get("OPENAI_API_KEY") and not refresh:
        _using_bedrock, _last_api_env_error = False, None
        return True
    try:
        region = current_region(None)
        os.environ["OPENAI_API_KEY"] = provide_token(region, cwd)
        os.environ["OPENAI_BASE_URL"] = bedrock_base_url(region)
    except Exception as exc:
        _last_api_env_error = str(exc)
        return False
    _using_bedrock, _last_api_env_error = True, None
    return True


def require_api_env(cwd=None):
    if ensure_api_env(cwd):
        return
    message = (
        "Missing API credentials.\n\n"
        "- set `OPENAI_API_KEY`, or\n"
        "- configure AWS CLI for Bedrock\n"
        "- `oy` uses the same AWS CLI auth and profile as `aws`"
    )
    if _last_api_env_error:
        message += f"\n- AWS CLI error: {_last_api_env_error}"
    abort(message)


def require_runtime(cwd=None):
    require_api_env(cwd)
    env = command_env(cwd)
    missing = [tool for tool in ("bash",) if not which(tool, env.get("PATH"))]
    if missing:
        abort(
            "Required tools are missing.\n\n"
            + "\n".join(
                f"- {tool}: install `{tool}` and make sure it is on PATH"
                for tool in missing
            )
        )


def get_client(async_=False):
    require_api_env(Path.cwd())
    cls = AsyncOpenAI if async_ else OpenAI
    return cls(
        api_key=str(os.environ["OPENAI_API_KEY"]),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        max_retries=3,
    )


def resolve_path(root, raw):
    """Resolve a path relative to root, preventing escape via .. traversal.

    Raises ValueError if the resolved path would escape root.
    This is a security measure to constrain file access to the workspace.
    """
    path = (root / raw).resolve()
    if path == root or root in path.parents:
        return path
    raise ValueError(f"Path traversal denied: '{raw}' escapes workspace")


def apply_exact_replace(text, old, new, replace_all=False):
    if not old:
        raise ValueError("replace operation old must not be empty")
    count = text.count(old)
    if count == 0:
        raise ValueError("replace target not found")
    if count > 1 and not replace_all:
        raise ValueError(
            "replace target matched multiple locations; set replace_all=true"
        )
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return updated, count


def note_tool(state, name, **details):
    if state["tool_calls"] >= state["max_tool_calls"]:
        raise ValueError(
            f"reached max tool calls ({state['max_tool_calls']}) without a final response"
        )
    state["tool_calls"] += 1
    parts = [
        inline_code(key.replace("_", "-"))
        if value is True
        else f"{key.replace('_', '-')}: {inline_code(preview(value, 50))}"
        for key, value in details.items()
        if value not in (None, "", False)
    ]
    detail_text = ", ".join(parts)
    message = f"tool {inline_code(name)}" + (f": {detail_text}" if detail_text else "")
    # Use bullet for mutating tools (apply, bash), plain for idempotent reads
    if name in {"apply", "bash"}:
        markdown(f"● {message}", stderr=True)
    else:
        markdown(message, stderr=True)


def tool_list(state, path=".", limit=200):
    note_tool(state, "list", path=path, limit=limit)
    target = resolve_path(state["root"], path)
    if not target.is_dir():
        raise ValueError("path is not a directory")
    text = (
        "\n".join(
            rel(state["root"], item) + ("/" if item.is_dir() else "")
            for item in sorted(target.iterdir(), key=lambda item: item.as_posix())[
                : max(limit, 1)
            ]
        )
        or "<empty directory>"
    )
    show(text, 1)
    return clip(text)


def tool_read(state, path, offset=1, limit=200):
    note_tool(state, "read", path=path, offset=offset, limit=limit)
    target = resolve_path(state["root"], path)
    if target.is_dir():
        return tool_list(state, path, limit)
    lines, start = (
        target.read_text(encoding="utf-8", errors="replace").splitlines(),
        max(offset, 1) - 1,
    )
    return clip(
        "\n".join(
            f"{i + 1}: {line}"
            for i, line in enumerate(lines[start : start + max(limit, 1)], start=start)
        )
        or "<empty file>"
    )


def tool_apply(state, operations):
    if isinstance(operations, dict):
        operations = [operations]
    if not isinstance(operations, list) or not operations:
        raise ValueError(
            "operations must be a non-empty array or a single operation object"
        )
    note_tool(state, "apply", operations=len(operations))
    root = state["root"]
    summaries = []
    for index, operation in enumerate(operations, 1):
        if not isinstance(operation, dict):
            raise ValueError(f"operation {index} must be an object")
        kind = operation.get("op")
        path = operation.get("path")
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"operation {index} is missing a valid op")
        if not isinstance(path, str) or not path:
            raise ValueError(f"operation {index} is missing a valid path")
        target = resolve_path(root, path)
        if kind == "replace":
            old = operation.get("old")
            new = operation.get("new")
            replace_all = operation.get("replace_all", False)
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError(
                    f"replace operation {index} requires string old and new"
                )
            if not isinstance(replace_all, bool):
                raise ValueError(
                    f"replace operation {index} replace_all must be boolean"
                )
            if not target.exists():
                raise ValueError(f"file does not exist: {rel(root, target)}")
            if target.is_dir():
                raise ValueError(
                    f"cannot replace text in directory: {rel(root, target)}"
                )
            updated, count = apply_exact_replace(
                target.read_text(encoding="utf-8", errors="replace"),
                old,
                new,
                replace_all,
            )
            target.write_text(updated, encoding="utf-8")
            summaries.append(
                f"replaced {rel(root, target)} ({count} match{'es' if count != 1 else ''})"
            )
            continue
        if kind == "write":
            content = operation.get("content")
            overwrite = operation.get("overwrite", False)
            if not isinstance(content, str):
                raise ValueError(f"write operation {index} requires string content")
            if not isinstance(overwrite, bool):
                raise ValueError(f"write operation {index} overwrite must be boolean")
            if target.exists() and target.is_dir():
                raise ValueError(f"cannot write directory: {rel(root, target)}")
            if target.exists() and not overwrite:
                raise ValueError(
                    f"file already exists: {rel(root, target)}; set overwrite=true"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            summaries.append(f"wrote {rel(root, target)}")
            continue
        if kind == "move":
            destination_raw = operation.get("to")
            if not isinstance(destination_raw, str) or not destination_raw:
                raise ValueError(f"move operation {index} requires a valid to path")
            if not target.exists():
                raise ValueError(f"file does not exist: {rel(root, target)}")
            if target.is_dir():
                raise ValueError(f"cannot move directory: {rel(root, target)}")
            destination = resolve_path(root, destination_raw)
            if destination == target:
                raise ValueError(
                    f"move destination matches source: {rel(root, target)}"
                )
            if destination.exists():
                raise ValueError(
                    f"destination already exists: {rel(root, destination)}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            target.rename(destination)
            summaries.append(f"moved {rel(root, target)} -> {rel(root, destination)}")
            continue
        if kind == "delete":
            if not target.exists():
                raise ValueError(f"file does not exist: {rel(root, target)}")
            if target.is_dir():
                raise ValueError(f"cannot delete directory: {rel(root, target)}")
            target.unlink()
            summaries.append(f"deleted {rel(root, target)}")
            continue
        raise ValueError(
            f"operation {index} has unsupported op {inline_code(kind)}; use replace, write, move, or delete"
        )
    out = "\n".join(summaries)
    show(out, 3)
    return out


def tool_bash(state, command, timeout_seconds=120):
    note_tool(state, "bash", command=command, timeout=timeout_seconds)
    env = command_env(state["root"])
    result = run_cmd(
        [which("bash", env.get("PATH")) or "bash", "-c", command],
        cwd=state["root"],
        env=env,
        timeout=timeout_seconds,
    )
    out = format_bash_result(command, result.returncode, result.stdout, result.stderr)
    out = clip(out, tail_chars=MAX_TOOL_OUTPUT_TAIL_CHARS)
    show(out, 8)
    return out


def tool_grep(state, pattern, path=".", file_glob=None):
    note_tool(state, "grep", pattern=pattern, path=path, glob=file_glob)
    env, target = command_env(state["root"]), resolve_path(state["root"], path)
    if not target.exists():
        raise ValueError(f"search path does not exist: {rel(state['root'], target)}")
    if target.is_file() and file_glob:
        raise ValueError("file_glob only works when path is a directory")
    search_path = str(target)
    for name, build in SEARCH_BACKENDS.items():
        if not (exe := which(name, env.get("PATH"))):
            continue
        result = run_cmd(
            build(exe, pattern, search_path, file_glob), cwd=state["root"], env=env
        )
        if result.returncode not in (0, 1):
            detail = (result.stderr or result.stdout or f"{name} failed").strip()
            raise ValueError(
                f"{name} search failed for {rel(state['root'], target)}: {detail}"
            )
        out = result.stdout.strip() or "<no matches>"
        show(out, 3)
        return clip(out)
    raise ValueError("grep requires `rg` or `grep` on PATH")


def tool_glob(state, pattern, path="."):
    note_tool(state, "glob", pattern=pattern, path=path)
    base = resolve_path(state["root"], path)
    out = (
        "\n".join(
            rel(state["root"], match) + ("/" if match.is_dir() else "")
            for match in sorted(base.glob(pattern), key=lambda item: item.as_posix())[
                :200
            ]
        )
        or "<no matches>"
    )
    show(out, 1)
    return clip(out)


def tool_httpx(
    state,
    url,
    preset=None,
    method=None,
    headers=None,
    params=None,
    body=None,
    json_body=None,
    timeout_seconds=20,
    response_mode="auto",
    json_path=None,
    max_chars=MAX_HTTPX_CHARS,
):
    if preset is not None and preset not in HTTPX_PRESET["enum"]:
        raise ValueError("preset must be one of page, json, or post_json")
    if not isinstance(method, str) and method is not None:
        raise ValueError("method must be a string")
    method = (
        (method or ("POST" if body is not None or json_body is not None else "GET"))
        .strip()
        .upper()
    )
    if preset == "post_json" and method == "GET":
        method = "POST"
    if response_mode == "auto" and preset in {"json", "post_json"}:
        response_mode = "json"
    elif response_mode == "body" and json_path:
        response_mode = "json"
    note_tool(
        state,
        "httpx",
        preset=preset,
        method=method,
        url=url,
        response_mode=response_mode,
        json_path=json_path,
        timeout=timeout_seconds,
        max_chars=max_chars,
    )
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("httpx only supports http and https")
    if body is not None and json_body is not None:
        raise ValueError("provide either body or json_body, not both")
    if not method:
        raise ValueError("method must be a non-empty string")
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if response_mode not in HTTPX_RESPONSE_MODE["enum"]:
        raise ValueError("response_mode must be one of auto, headers, body, or json")
    if json_path is not None and not isinstance(json_path, str):
        raise ValueError("json_path must be a string")
    if response_mode == "headers" and json_path:
        raise ValueError("json_path requires body or json output")
    if body is not None and not isinstance(body, str):
        raise ValueError("body must be a string")
    if json_body is not None and not isinstance(
        json_body, (dict, list, str, int, float, bool)
    ):
        raise ValueError("json_body must be valid JSON-like data")
    request_headers = normalize_mapping(headers, "headers")
    request_params = normalize_mapping(params, "params")
    status("Fetching HTTP content.")
    try:
        with httpx.Client(
            follow_redirects=True, timeout=float(timeout_seconds)
        ) as http:
            response = http.request(
                method,
                parsed.geturl(),
                headers=request_headers,
                params=request_params,
                content=body,
                json=json_body,
            )
    except httpx.HTTPError as exc:
        raise ValueError(httpx_error_message(exc, timeout_seconds)) from exc
    out = render_httpx_output(response, response_mode, json_path=json_path)
    show(out, 1)
    return clip(out, max_chars)


def tool_ask(state, question, choices=None):
    note_tool(state, "ask", question=question, choices=choices)
    if not sys.stdin.isatty():
        raise ValueError("Cannot ask question: stdin is not a TTY")
    prompt_text(question)
    if not choices:
        return Prompt.ask("Answer", console=STDERR).strip()
    markdown(
        "## Options\n\n"
        + "\n".join(
            f"{i}. {inline_code(choice)}" for i, choice in enumerate(choices, 1)
        ),
        stderr=True,
    )
    while True:
        response = Prompt.ask("Selection", console=STDERR).strip()
        if response.isdigit() and 0 < int(response) <= len(choices):
            return choices[int(response) - 1]
        if response in choices:
            return response
        warning(f"Enter a number from 1 to {len(choices)} or an exact choice.")


TOOL_SPECS = {
    "apply": (
        tool_apply,
        "Apply structured file operations in the workspace. Operations: "
        "replace (exact string match), write (new file, use overwrite=true for existing), "
        "move (rename), delete (remove). Batch related operations. Read files first.",
        {"operations": APPLY_OPERATIONS},
        ["operations"],
    ),
    "list": (
        tool_list,
        "List directory contents. Use to discover structure before exploring deeper. "
        "Sorted alphabetically, trailing / for directories. Prefer over glob for unfamiliar trees.",
        {"path": STR, "limit": INT},
        [],
    ),
    "bash": (
        tool_bash,
        "Run shell commands: builds, tests, git, npm/pip/make, scripts. "
        "Not for reading files (use read) or searching (use grep). Shows stdout and stderr.",
        {"command": STR, "timeout_seconds": INT},
        ["command"],
    ),
    "read": (
        tool_read,
        "Read a file or directory. ALWAYS read before editing. "
        "Use offset/limit for large files. Line numbers included. Primary inspection tool.",
        {"path": STR, "offset": INT, "limit": INT},
        ["path"],
    ),
    "grep": (
        tool_grep,
        "Search file contents by text/regex. Use when you know what to find but not where. "
        "Use file_glob to filter by extension (e.g., '*.py'). Returns lines with file:line.",
        {"pattern": STR, "path": STR, "file_glob": STR},
        ["pattern"],
    ),
    "glob": (
        tool_glob,
        "Find files by name pattern ('*.py', 'src/**/*.js'). Use when you know the pattern. "
        "For exploring structure, use list instead. Supports *, ?, **.",
        {"pattern": STR, "path": STR},
        ["pattern"],
    ),
    "httpx": (
        tool_httpx,
        "Fetch web pages or APIs. Use to get documentation, standards, or API data. "
        "Presets: 'page' (HTML→markdown), 'json' (API response), 'post_json' (POST JSON). "
        "Use json_path for nested extraction. Headers like Authorization are redacted.",
        {
            "url": STR,
            "preset": HTTPX_PRESET,
            "method": STR,
            "headers": MAP,
            "params": MAP,
            "body": STR,
            "json_body": ANY_JSON,
            "timeout_seconds": INT,
            "response_mode": HTTPX_RESPONSE_MODE,
            "json_path": STR,
            "max_chars": INT,
        },
        ["url"],
    ),
    "ask": (
        tool_ask,
        "Ask the user a question. Use for plan approvals, ambiguous decisions, checkpoints. "
        "Provide choices for multiple-choice. Only available in interactive mode. "
        "Batch work and ask at meaningful checkpoints, not after every trivial step.",
        {"question": STR, "choices": STRINGS},
        ["question"],
    ),
}


def run_is_interactive(non_interactive: bool = False) -> bool:
    return sys.stdin.isatty() and not non_interactive


def active_system_prompt(interactive):
    return BASE_SYSTEM_PROMPT + (
        INTERACTIVE_SYSTEM_PROMPT if interactive else NONINTERACTIVE_SYSTEM_PROMPT
    )


def active_tool_specs(interactive):
    return (
        TOOL_SPECS
        if interactive
        else {name: spec for name, spec in TOOL_SPECS.items() if name != "ask"}
    )


def chat_tools(tool_specs):
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        }
        for name, (_, desc, props, required) in tool_specs.items()
    ]


def parse_tool_arguments(args_str: str) -> dict[str, Any]:
    """Parse tool arguments from LLM output.

    Handles malformed output where some LLMs emit duplicated JSON (same object twice).
    Hunts for valid JSON starting near the midpoint if initial parse fails.
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
                except (json.JSONDecodeError, ValueError):
                    pass
        raise exc


def run_tool(state, tool_name, tool_args):
    """Execute a tool call with the given arguments.

    Returns a string result. Errors are caught and returned as error messages
    rather than raised, to keep the agent loop running.
    """
    name = tool_name[5:] if tool_name.startswith("tool_") else tool_name
    tool_specs = state.get("tool_specs", TOOL_SPECS)
    if name not in tool_specs:
        return f"Error: Tool '{name}' is unavailable in this run"
    try:
        return str(tool_specs[name][0](state, **tool_args))
    except Exception as exc:
        return f"Error in {name}: {type(exc).__name__}: {exc}"


def message_size(msg) -> int:
    """Calculate the approximate character size of a message."""
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            # For multimodal content, sum text lengths
            total = 0
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        total += len(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        # Base64 images are usually large, estimate from URL data
                        url_data = item.get("image_url", {}).get("url", "")
                        if url_data.startswith("data:"):
                            total += len(url_data.split(",", 1)[-1])
                        else:
                            total += len(url_data)
                    else:
                        total += len(json.dumps(item))
                else:
                    total += len(str(item))
            return total
        return len(json.dumps(content, ensure_ascii=True))
    return len(str(msg))


def session_size(messages: list) -> int:
    """Calculate total size of messages in a session."""
    return sum(message_size(msg) for msg in messages)


def format_size(chars: int) -> str:
    """Format character count as human-readable size."""
    result = filesize.decimal(chars)
    if result.endswith(" bytes"):
        return result.replace(" bytes", " chars")
    return result.replace("B", " chars")


def list_model_ids():
    require_api_env()
    status("Loading available models.")
    return sorted(model.id for model in list(cast(OpenAI, get_client()).models.list()))


async def run_turn(client, messages, state, model, tool_defs, max_steps):
    for _ in range(max_steps):
        size = session_size(messages)
        size_str = format_size(size)
        spinner = Status(
            f"Waiting for {model} · {size_str}",
            console=STDERR,
            spinner="dots",
        )
        spinner.start()
        try:
            response = await cast(AsyncOpenAI, client).chat.completions.create(
                model=model,
                messages=messages,
                tools=tool_defs,
                tool_choice="auto",
            )  # type: ignore[arg-type]
        finally:
            spinner.stop()
        message = response.choices[0].message.model_dump(exclude_none=True)
        calls = []
        for call in message.get("tool_calls") or []:
            if call.get("type") != "function":
                continue
            function = call["function"]
            tool_args = parse_tool_arguments(function["arguments"])
            function["arguments"] = json.dumps(tool_args)
            calls.append((call["id"], function["name"], tool_args))
        output = message.get("content") or ""
        output = (
            output if isinstance(output, str) else json.dumps(output, ensure_ascii=True)
        )
        if calls:
            messages.append(message)
            results = [
                (call_id, run_tool(state, name, args)) for call_id, name, args in calls
            ]
            messages.extend(
                {"role": "tool", "tool_call_id": call_id, "content": result}
                for call_id, result in results
            )
            continue
        render_markdown(output)
        return 0, output
    return fail(f"reached max steps ({max_steps}) without a final response"), ""


async def run_agent(
    prompt, model, root, system_prompt, max_steps, max_tool_calls, interactive
):
    tool_specs = active_tool_specs(interactive)
    state = {
        "root": root,
        "tool_calls": 0,
        "max_tool_calls": max_tool_calls,
        "tool_specs": tool_specs,
    }
    tool_defs = chat_tools(tool_specs)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    async def run_with_client(client):
        return await run_turn(client, messages, state, model, tool_defs, max_steps)

    try:
        return await run_with_client(get_client(async_=True))
    except (AuthenticationError, PermissionDeniedError) as exc:
        if ensure_api_env(root, refresh=True):
            warning("Bedrock token expired. Refreshing credentials.")
            try:
                return await run_with_client(get_client(async_=True))
            except (AuthenticationError, PermissionDeniedError) as retry_exc:
                kind = (
                    "authentication"
                    if isinstance(retry_exc, AuthenticationError)
                    else "permission"
                )
                return fail(f"API {kind} error: {retry_exc}"), ""
            except Exception as retry_exc:
                return fail(str(retry_exc)), ""
        kind = (
            "authentication" if isinstance(exc, AuthenticationError) else "permission"
        )
        return fail(f"API {kind} error: {exc}"), ""
    except RateLimitError as exc:
        return fail(f"API rate limit: {exc}"), ""
    except BadRequestError as exc:
        return fail(f"API bad request: {exc}"), ""
    except Exception as exc:
        return fail(str(exc)), ""


def read_system_prompt(system_file, interactive):
    system_prompt = active_system_prompt(interactive)
    if system_file is None:
        return system_prompt
    if not system_file.exists():
        abort(f"System file does not exist: {inline_code(system_file)}")
    if system_file.is_dir():
        abort(f"System file is a directory: {inline_code(system_file)}")
    extra = ""
    try:
        extra = system_file.read_text(encoding="utf-8")
    except OSError as exc:
        abort(f"Could not read system file {inline_code(system_file)}: {exc}")
    return system_prompt + "\n\n" + extra


def audit(prompt: str = ""):
    """Run a security and complexity audit of the repository.

    :param prompt: Additional audit focus instructions.
    """

    workspace = current_workspace().resolve()
    if not workspace.is_dir():
        abort(f"Workspace root is not a directory: {inline_code(workspace)}")
    require_runtime(workspace)
    chosen_model = current_model(None)

    audit_prompt = "Conduct a security and complexity audit."
    if prompt:
        audit_prompt += f" Additional focus: {prompt}"

    intro = [
        "## Audit",
        "",
        f"- workspace: {inline_code(workspace)}",
        f"- model: {inline_code(chosen_model)}",
        f"- mode: {inline_code('non-interactive')}",
    ]
    if prompt:
        intro.append(f"- focus: {inline_code(preview(prompt, 100))}")
    markdown("\n".join(intro), stderr=True)

    code, _ = asyncio.run(
        run_agent(
            audit_prompt,
            chosen_model,
            workspace,
            AUDIT_SYSTEM_PROMPT,
            DEFAULT_MAX_STEPS,
            DEFAULT_MAX_TOOL_CALLS,
            interactive=False,
        )
    )

    return code


def run(
    *prompt: str,
):
    """Run the coding assistant in a workspace.

    :param prompt: Prompt text to send. Reads stdin if omitted.
    """

    task = (
        " ".join(prompt)
        if prompt
        else (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    )
    if not task:
        abort("Provide a prompt argument or pipe one on stdin.")
    workspace = current_workspace().resolve()
    if not workspace.is_dir():
        abort(f"Workspace root is not a directory: {inline_code(workspace)}")
    require_runtime(workspace)
    chosen_model = current_model(None)
    system_file = current_system_file()
    interactive = run_is_interactive(current_non_interactive())
    system_prompt = read_system_prompt(system_file, interactive)
    intro = [
        "## Run",
        "",
        f"- workspace: {inline_code(workspace)}",
        f"- model: {inline_code(chosen_model)}",
        f"- mode: {inline_code('interactive' if interactive else 'non-interactive')}",
        f"- prompt: {inline_code(preview(task, 100))}",
    ]
    if system_file is not None:
        intro.append(f"- system file: {inline_code(system_file.resolve())}")
    markdown("\n".join(intro), stderr=True)
    return asyncio.run(
        run_agent(
            task,
            chosen_model,
            workspace,
            system_prompt,
            DEFAULT_MAX_STEPS,
            DEFAULT_MAX_TOOL_CALLS,
            interactive,
        )
    )[0]


def render_model_list(
    items, *, title, query=None, current=None, stderr=False, limit=None
):
    shown = list(items if limit is None else items[:limit])
    lines = [title]
    if current:
        lines.extend(["", f"- current model: {inline_code(current)}"])
    if query:
        lines.extend(["", f"- filter: {inline_code(query)}"])
    if shown:
        lines.extend(
            ["", *[f"{i}. {inline_code(item)}" for i, item in enumerate(shown, 1)]]
        )
    else:
        lines.extend(["", "- no matching models"])
    if len(items) > len(shown):
        lines.extend(["", f"- showing {len(shown)} of {len(items)} matches"])
    markdown("\n".join(lines), stderr=stderr)


def filter_models(items, query):
    needle = query.strip().lower()
    return [item for item in items if needle in item.lower()]


def select_model_by_number(items, value):
    if not value.isdigit():
        return None
    index = int(value)
    if 1 <= index <= len(items):
        return items[index - 1]
    return None


def resolve_model_choice(model_id=None):
    available = list_model_ids()
    current = current_model(None)
    if model_id in available:
        return model_id
    if model_id and not sys.stdin.isatty():
        matches = filter_models(available, model_id)
        if matches:
            render_model_list(
                matches,
                title="## Matching Models",
                query=model_id,
                current=current,
                stderr=True,
            )
        abort(
            f"No exact model match for {inline_code(model_id)}. Re-run in a TTY to filter and choose interactively."
        )
    if not sys.stdin.isatty():
        return None
    markdown(
        "## Choose a Model\n\n- Enter an exact model ID to save it.\n- Enter text to filter the list.\n- Enter a number to pick from the currently listed models.",
        stderr=True,
    )
    if model_id is None:
        render_model_list(
            available,
            title="## Available Models",
            current=current,
            stderr=True,
        )
    shown = available
    query = (
        model_id
        or Prompt.ask("Model or filter", console=STDERR, default=current).strip()
    )
    while True:
        query = query.strip() or current
        if query in available:
            return query
        if choice := select_model_by_number(shown, query):
            return choice
        matches = filter_models(available, query)
        render_model_list(
            matches,
            title="## Matching Models",
            query=query,
            current=current,
            stderr=True,
        )
        shown = matches
        query = Prompt.ask("Model or filter", console=STDERR).strip()


def bedrock_token(*, region: str | None = None):
    """Print export statements for Bedrock-backed OpenAI credentials.

    :param region: AWS region to use when generating the token.
    """

    chosen = current_region(region)
    status(f"Generating Bedrock credentials for {inline_code(chosen)}.")
    token = provide_token(chosen, cwd=Path.cwd())
    render_markdown(
        "## Bedrock Credentials\n\n"
        + "Paste this into another shell if you want to reuse the current Bedrock session.\n\n"
        + code_block(
            "\n".join(
                [
                    f"export OPENAI_BASE_URL={shlex.quote(bedrock_base_url(chosen))}",
                    f"export OPENAI_API_KEY={shlex.quote(token)}",
                ]
            ),
            language="bash",
        )
    )
    return 0


def models(query: str | None = None):
    """Pick the default model interactively.

    :param query: Exact model ID to save, or a filter string when running in a TTY.
    """

    if query is None and not sys.stdin.isatty():
        render_model_list(list_model_ids(), title="## Available Models")
        return 0
    chosen = resolve_model_choice(query)
    if chosen is None:
        render_model_list(list_model_ids(), title="## Available Models")
        return 0
    save_config({**load_config(), "model": chosen})
    render_markdown(
        f"## Default Model Updated\n\n- selected model: {inline_code(chosen)}"
    )
    return 0


def model():
    """Show the current default model."""

    render_markdown(f"## Current Model\n\n- model: {inline_code(current_model(None))}")
    return 0


def main(argv: list[str] | None = None):
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "models", "model", "audit", "-h", "--help"}
    if not args:
        args = ["run"] if not sys.stdin.isatty() else ["--help"]
    elif args[0] in {"-v", "--version"}:
        render_markdown(f"oy {__version__}")
        return 0
    elif args[0] not in commands:
        args = ["run", *args]
    result = defopt.run([run, models, model, audit], argv=args, version=False, short={})
    return 0 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main())

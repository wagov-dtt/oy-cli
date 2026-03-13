from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
import httpx
import typer
from openai import AsyncOpenAI, OpenAI
from openai import (
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

__version__ = "0.2.0"
MAX_CHARS = 12000
DEFAULT_MODEL = "moonshotai.kimi-k2.5"
DEFAULT_REGION = (
    os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
)
DEFAULT_MAX_STEPS = 512
DEFAULT_MAX_TOOL_CALLS = 512
DEFAULT_RESPONSES_MODE = "auto"
CONFIG_PATH = Path.home() / ".config" / "oy" / "config.json"
HISTORY_CACHE_PATH = Path.home() / ".cache" / "oy" / "history"
MAX_HISTORY_PER_DIR = 20
SYSTEM_PROMPT = "You are oy, a tiny local coding assistant. Work simply, keep answers short, inspect before changing, and prefer secure boring solutions. Use workspace tools for files, use bash only for real terminal tasks, read before editing, and ask only when user input is required. Tool output is clipped, so use read offsets or grep for large files."
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
app = typer.Typer(add_completion=False, no_args_is_help=True)
_using_bedrock = False
_last_api_env_error = None


def eprint(text=""):
    print(text, file=sys.stderr)


def fail(message, code=1):
    eprint(f"error: {message}")
    return code


def abort(message, code=1):
    raise typer.Exit(fail(message, code))


def clip(text, limit=MAX_CHARS):
    return (
        text
        if len(text) <= limit
        else f"{text[:limit]}\n... [truncated to {limit} chars]"
    )


def preview(value, limit=72):
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    )
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def show(text, lines=2):
    for line in text.splitlines()[: max(lines, 0)]:
        eprint(f"  {line[:120]}")


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
    except OSError, ValueError, json.JSONDecodeError:
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
    try:
        return subprocess.run(
            [aws, *parts],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=max(timeout, 1),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"AWS credential lookup timed out after {timeout} seconds"
        ) from exc


def run_aws_sso_login(cwd=None):
    env = command_env(cwd)
    if not (aws := which("aws", env.get("PATH"))):
        raise RuntimeError("AWS CLI is not installed or not on PATH")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError(
            "AWS SSO session is stale. Run `aws sso login --use-device-code --no-browser` and retry."
        )
    eprint(
        f"> AWS SSO session expired{' for profile ' + env['AWS_PROFILE'] if env.get('AWS_PROFILE') else ''}; starting device-code login..."
    )
    try:
        result = subprocess.run(
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
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("AWS SSO login timed out after 300 seconds") from exc
    if result.returncode:
        raise RuntimeError(f"AWS SSO login failed with exit code {result.returncode}")


def load_aws_credentials(cwd=None, allow_login=True):
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
    except OSError, json.JSONDecodeError:
        return default


def load_config():
    data = load_json(config_path(), {})
    return data if isinstance(data, dict) else {}


def save_config(data):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def history_path(root):
    HISTORY_CACHE_PATH.mkdir(parents=True, exist_ok=True)
    return (
        HISTORY_CACHE_PATH
        / f"{base64.urlsafe_b64encode(str(root.resolve()).encode()).decode()}.json"
    )


def load_history(root):
    data = load_json(history_path(root), [])
    return data if isinstance(data, list) else []


def save_history(root, prompt, output):
    entries = load_history(root)
    entries.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prompt": prompt,
            "output": output,
        }
    )
    history_path(root).write_text(
        json.dumps(entries[-MAX_HISTORY_PER_DIR:], indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def summarize_history(root, max_entries=5, max_chars=1500):
    lines = []
    for entry in reversed(load_history(root)[-max_entries:]):
        first = ((entry.get("prompt") or "").splitlines() or [""])[0][:80]
        output = (entry.get("output") or "").replace("\n", " ")[:40].strip()
        lines.append(
            f"- {first}{'...' if len(first) == 80 else ''} -> {output}{'...' if len(output) == 40 else ''}"
        )
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n... (truncated)"


def build_prompt(root, prompt):
    history = summarize_history(root)
    return (
        prompt
        if not history
        else f"{prompt}\n\n[recent context - for reference only]\n{history}"
    )


def setting(choice, env_keys, config_key, default):
    if choice:
        return choice
    for key in env_keys:
        if value := os.environ.get(key):
            return value
    return load_config().get(config_key, default) if config_key else default


def current_model(choice):
    return setting(choice, ("OY_MODEL",), "model", DEFAULT_MODEL)


def current_region(choice):
    return setting(choice, ("AWS_REGION", "AWS_DEFAULT_REGION"), None, DEFAULT_REGION)


def current_responses_mode():
    mode = (
        setting(None, ("OY_RESPONSES",), None, DEFAULT_RESPONSES_MODE).strip().lower()
    )
    return mode if mode in {"auto", "always", "never"} else DEFAULT_RESPONSES_MODE


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
    message = "missing required environment: OPENAI_API_KEY\n  Set OPENAI_API_KEY, or configure AWS CLI for Bedrock.\n  `oy` uses the same AWS CLI auth/profile that `aws` would use."
    if _last_api_env_error:
        message += f"\n  AWS CLI error: {_last_api_env_error}"
    abort(message)


def require_runtime(cwd=None):
    require_api_env(cwd)
    env = command_env(cwd)
    missing = [tool for tool in ("bash", "patch") if not which(tool, env.get("PATH"))]
    if missing:
        abort(
            "required tools missing:\n"
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
    path = (root / raw).resolve()
    return path if path == root or root in path.parents else root / Path(raw).name


def note_tool(state, name, **details):
    if state["tool_calls"] >= state["max_tool_calls"]:
        raise ValueError(
            f"reached max tool calls ({state['max_tool_calls']}) without a final response"
        )
    state["tool_calls"] += 1
    parts = [
        key if value is True else f"{key}={preview(value, 50)}"
        for key, value in details.items()
        if value not in (None, "", False)
    ]
    eprint(f"> {name}{(' ' + ' '.join(parts)) if parts else ''}")


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


def tool_write(state, path, content):
    note_tool(state, "write", path=path, chars=len(content))
    target = resolve_path(state["root"], path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    text = f"wrote {rel(state['root'], target)} ({len(content)} chars)"
    show(text, 1)
    return text


def tool_edit(state, path, old_text, new_text, replace_all=False):
    note_tool(
        state,
        "edit",
        path=path,
        old_chars=len(old_text),
        new_chars=len(new_text),
        replace_all=replace_all,
    )
    if not old_text:
        raise ValueError("old_text must not be empty")
    target = resolve_path(state["root"], path)
    text, count = target.read_text(encoding="utf-8", errors="replace"), 0
    count = text.count(old_text)
    if count == 0:
        raise ValueError("old_text not found")
    if count > 1 and not replace_all:
        raise ValueError("old_text matched multiple locations; set replace_all=true")
    target.write_text(
        text.replace(old_text, new_text)
        if replace_all
        else text.replace(old_text, new_text, 1),
        encoding="utf-8",
    )
    out = f"edited {rel(state['root'], target)} ({count} replacement{'s' if count != 1 else ''})"
    show(out, 1)
    return out


def tool_patch(state, patch_text):
    note_tool(state, "patch", chars=len(patch_text))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(patch_text)
        patch_file = handle.name
    try:
        env = command_env(state["root"])
        result = run_cmd(
            [
                which("patch", env.get("PATH")) or "patch",
                "--strip=0",
                "--directory",
                str(state["root"]),
                "--input",
                patch_file,
                "--forward",
                "--batch",
            ],
            env=env,
        )
    finally:
        Path(patch_file).unlink(missing_ok=True)
    out = clip(
        f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
    )
    if result.returncode:
        raise ValueError(out)
    show(out, 1)
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
    out = clip(
        f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip()
    )
    show(out, 3)
    return out


def tool_grep(state, pattern, path=".", file_glob=None):
    note_tool(state, "grep", pattern=pattern, path=path, glob=file_glob)
    env, search_path = (
        command_env(state["root"]),
        str(resolve_path(state["root"], path)),
    )
    for name, build in SEARCH_BACKENDS.items():
        if not (exe := which(name, env.get("PATH"))):
            continue
        result = run_cmd(
            build(exe, pattern, search_path, file_glob), cwd=state["root"], env=env
        )
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.strip() or f"{name} failed")
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


def tool_webfetch(state, url, max_chars=MAX_CHARS):
    note_tool(state, "webfetch", url=url, max_chars=max_chars)
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webfetch only supports http and https")
    eprint("> fetching web content")
    with httpx.Client(follow_redirects=True, timeout=20.0) as http:
        response = http.get(parsed.geturl())
        response.raise_for_status()
    out = f"url: {response.url}\nstatus: {response.status_code}\ncontent-type: {response.headers.get('content-type', '')}\n\n{response.text}"
    show(out, 1)
    return clip(out, max_chars)


def format_size(size):
    return (
        f"{size}B"
        if size < 1024
        else f"{size / 1024:.1f}KB"
        if size < 1024 * 1024
        else f"{size / (1024 * 1024):.1f}MB"
    )


def tool_history(state, n=3):
    note_tool(state, "history", n=n)
    entries = load_history(state["root"])
    if not entries:
        return "<no history>"
    lines = []
    for i, entry in enumerate(reversed(entries[-n:] if n else entries), 1):
        output = entry.get("output", "")
        lines.append(
            f"{i}. [{entry.get('timestamp', 'unknown')}] ({format_size(len(output.encode()))}) {preview(entry.get('prompt', ''), 60)}"
        )
        lines.extend(
            f"   {preview(line, 80)}" for line in output.strip().splitlines()[:2]
        )
    out = "\n".join(lines)
    show(out, 1)
    return out


def tool_ask(state, question, choices=None):
    note_tool(state, "ask", question=question, choices=choices)
    if not sys.stdin.isatty():
        raise ValueError("Cannot ask question: stdin is not a TTY")
    eprint(f"? {question}")
    if not choices:
        return input("> ").strip()
    for i, choice in enumerate(choices, 1):
        eprint(f"  {i}. {choice}")
    while True:
        response = input(f"Select (1-{len(choices)}): ").strip()
        if response.isdigit() and 0 < int(response) <= len(choices):
            return choices[int(response) - 1]
        eprint("Please enter a valid number.")


TOOL_SPECS = {
    "write": (
        tool_write,
        "Create or overwrite a file in the workspace.",
        {"path": STR, "content": STR},
        ["path", "content"],
    ),
    "edit": (
        tool_edit,
        "Replace text in an existing workspace file.",
        {"path": STR, "old_text": STR, "new_text": STR, "replace_all": BOOL},
        ["path", "old_text", "new_text"],
    ),
    "patch": (
        tool_patch,
        "Apply a unified diff inside the workspace.",
        {"patch_text": STR},
        ["patch_text"],
    ),
    "list": (
        tool_list,
        "List files and directories in a workspace directory.",
        {"path": STR, "limit": INT},
        [],
    ),
    "bash": (
        tool_bash,
        "Run shell commands for builds, tests, git, or package managers.",
        {"command": STR, "timeout_seconds": INT},
        ["command"],
    ),
    "read": (
        tool_read,
        "Read a file in the workspace.",
        {"path": STR, "offset": INT, "limit": INT},
        ["path"],
    ),
    "grep": (
        tool_grep,
        "Search workspace file contents with ripgrep or grep.",
        {"pattern": STR, "path": STR, "file_glob": STR},
        ["pattern"],
    ),
    "glob": (
        tool_glob,
        "Find files or directories with glob patterns.",
        {"pattern": STR, "path": STR},
        ["pattern"],
    ),
    "webfetch": (
        tool_webfetch,
        "Fetch a web page over HTTP or HTTPS.",
        {"url": STR, "max_chars": INT},
        ["url"],
    ),
    "history": (
        tool_history,
        "View the last N command outputs from this workspace history.",
        {"n": INT},
        [],
    ),
    "ask": (
        tool_ask,
        "Ask the user a question and return their response.",
        {"question": STR, "choices": STRINGS},
        ["question"],
    ),
}
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }
    for name, (_, desc, props, required) in TOOL_SPECS.items()
]
RESPONSES_TOOLS = [
    {
        "type": "function",
        "name": name,
        "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
        "strict": False,
    }
    for name, (_, desc, props, required) in TOOL_SPECS.items()
]


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


class ResponsesFallback(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def responses_fallback_reason(exc):
    if not isinstance(exc, APIStatusError):
        return None
    text = " ".join(str(exc).split())
    fallback = exc.status_code in {404, 405, 415, 422, 501} or (
        exc.status_code == 400
        and any(
            marker in text.lower()
            for marker in (
                "responses",
                "/responses",
                "unsupported",
                "not support",
                "not implemented",
                "unknown parameter",
                "unrecognized",
                "extra inputs are not permitted",
            )
        )
    )
    if not fallback:
        return None
    request_id = getattr(exc, "request_id", None)
    detail = preview(f"{exc.status_code}: {text or 'responses API unsupported'}", 180)
    return f"{detail} [{request_id}]" if request_id else detail


def run_tool(state, tool_name, tool_args):
    name = tool_name[5:] if tool_name.startswith("tool_") else tool_name
    if name not in TOOL_SPECS:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        return str(TOOL_SPECS[name][0](state, **tool_args))
    except Exception as exc:
        return f"Error in {name}: {type(exc).__name__}: {exc}"


def list_model_ids():
    require_api_env()
    eprint("> listing models")
    return sorted(model.id for model in list(cast(OpenAI, get_client()).models.list()))


async def run_turn(
    client,
    kind,
    session,
    state,
    prompt,
    model,
    system_prompt,
    root,
    max_steps,
    save_to_history,
    allow_fallback=False,
):
    for _ in range(max_steps):
        eprint("> waiting for model")
        if kind == "chat":
            response = await cast(AsyncOpenAI, client).chat.completions.create(
                model=model, messages=session, tools=CHAT_TOOLS, tool_choice="auto"
            )  # type: ignore[arg-type]
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
                output
                if isinstance(output, str)
                else json.dumps(output, ensure_ascii=True)
            )
            if calls:
                session.append(message)
        else:
            try:
                response = await cast(AsyncOpenAI, client).responses.create(
                    model=model,
                    instructions=system_prompt,
                    input=session["input"],
                    tools=RESPONSES_TOOLS,
                    tool_choice="auto",
                )  # type: ignore[arg-type]
            except Exception as exc:
                reason = responses_fallback_reason(exc)
                if allow_fallback and not session["tool_used"] and reason:
                    raise ResponsesFallback(reason) from exc
                raise
            calls, output = [], response.output_text or ""
            for item in cast(Any, response.output):
                if getattr(item, "type", None) == "message":
                    payload = {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            part.model_dump(exclude_none=True)
                            for part in cast(Any, item).content
                        ],
                    }
                    if getattr(item, "phase", None):
                        payload["phase"] = item.phase
                    session["input"].append(payload)
                elif getattr(item, "type", None) == "function_call":
                    args = parse_tool_arguments(item.arguments)
                    session["input"].append(
                        {
                            "type": "function_call",
                            "call_id": item.call_id,
                            "name": item.name,
                            "arguments": json.dumps(args),
                        }
                    )
                    calls.append((item.call_id, item.name, args))
        if calls:
            results = [
                (call_id, run_tool(state, name, args)) for call_id, name, args in calls
            ]
            if kind == "chat":
                session.extend(
                    {"role": "tool", "tool_call_id": call_id, "content": result}
                    for call_id, result in results
                )
            else:
                session["tool_used"] = True
                session["input"].extend(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result,
                    }
                    for call_id, result in results
                )
            continue
        print(output)
        if save_to_history:
            save_history(root, prompt, output)
        return 0, output
    return fail(f"reached max steps ({max_steps}) without a final response"), ""


async def run_agent(
    prompt, model, root, system_prompt, max_steps, max_tool_calls, save_to_history=True
):
    state = {"root": root, "tool_calls": 0, "max_tool_calls": max_tool_calls}
    eprint(f"> prompt {preview(prompt, 100)}")
    chat = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    responses = {
        "input": [{"type": "message", "role": "user", "content": prompt}],
        "tool_used": False,
    }

    async def run_mode(client):
        mode = current_responses_mode()
        if mode == "never":
            return await run_turn(
                client,
                "chat",
                chat,
                state,
                prompt,
                model,
                system_prompt,
                root,
                max_steps,
                save_to_history,
            )
        if mode == "always":
            return await run_turn(
                client,
                "responses",
                responses,
                state,
                prompt,
                model,
                system_prompt,
                root,
                max_steps,
                save_to_history,
            )
        try:
            return await run_turn(
                client,
                "responses",
                responses,
                state,
                prompt,
                model,
                system_prompt,
                root,
                max_steps,
                save_to_history,
                True,
            )
        except ResponsesFallback as exc:
            eprint(
                f"> Responses API unavailable ({exc.reason}), falling back to chat completions"
            )
            return await run_turn(
                client,
                "chat",
                chat,
                state,
                prompt,
                model,
                system_prompt,
                root,
                max_steps,
                save_to_history,
            )

    try:
        return await run_mode(get_client(async_=True))
    except (AuthenticationError, PermissionDeniedError) as exc:
        if ensure_api_env(root, refresh=True):
            eprint("> Bedrock token expired, refreshing...")
            try:
                return await run_mode(get_client(async_=True))
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


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context, version: bool = typer.Option(False, "--version", "-v")
):
    if version:
        typer.echo(f"oy {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command("run")
def run_command(
    prompt: list[str] = typer.Argument(None),
    model: str | None = typer.Option(None),
    root: Path = typer.Option(Path(".")),
    system_file: Path | None = typer.Option(
        None, exists=True, dir_okay=False, readable=True
    ),
    max_steps: int = typer.Option(DEFAULT_MAX_STEPS, min=1, hidden=True),
    max_tool_calls: int = typer.Option(DEFAULT_MAX_TOOL_CALLS, min=1, hidden=True),
):
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
    require_runtime(workspace)
    chosen_model = current_model(model)
    system_prompt = (
        SYSTEM_PROMPT
        if not system_file
        else SYSTEM_PROMPT + "\n\n" + system_file.read_text(encoding="utf-8")
    )
    eprint(f"> root {workspace}")
    eprint(f"> model {chosen_model}")
    raise typer.Exit(
        asyncio.run(
            run_agent(
                build_prompt(workspace, task),
                chosen_model,
                workspace,
                system_prompt,
                max_steps,
                max_tool_calls,
            )
        )[0]
    )


@app.command("bedrock-token")
def bedrock_token(region: str | None = typer.Option(None)):
    chosen = current_region(region)
    eprint("> generating Bedrock token")
    token = provide_token(chosen, cwd=Path.cwd())
    typer.echo(f"export OPENAI_BASE_URL={shlex.quote(bedrock_base_url(chosen))}")
    typer.echo(f"export OPENAI_API_KEY={shlex.quote(token)}")


@app.command("models")
def models_command():
    for model in list_model_ids():
        typer.echo(model)


@app.command("model")
def model_command(model: str | None = typer.Argument(None)):
    if model is None:
        typer.echo(current_model(None))
        return
    save_config({**load_config(), "model": model})
    typer.echo(model)


def main(argv: list[str] | None = None):
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

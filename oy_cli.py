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
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
import defopt
import httpx
from markdownify import markdownify as html_to_markdown
from openai import AsyncOpenAI, OpenAI
from openai import (
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt

__version__ = "0.2.0"
# Per-tool payloads should stay comfortable for long sessions on 128k-ish models.
MAX_TOOL_OUTPUT_CHARS = 16000
MAX_TOOL_OUTPUT_TAIL_CHARS = 4000
MAX_WEBFETCH_CHARS = 20000
DEFAULT_MODEL = "moonshotai.kimi-k2.5"
DEFAULT_REGION = (
    os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
)
DEFAULT_MAX_STEPS = 512
DEFAULT_MAX_TOOL_CALLS = 512
DEFAULT_RESPONSES_MODE = "auto"
CONFIG_PATH = Path.home() / ".config" / "oy" / "config.json"
BASE_SYSTEM_PROMPT = (
    "You are oy, a tiny local coding assistant. "
    "Work simply, inspect before changing, and prefer secure boring solutions. "
    "Use workspace tools for files, use bash only for real terminal tasks, and read before editing. "
    "Each run is a single fresh session: do not assume hidden memory, saved history, or prior conversation state beyond the current prompt and tool results. "
    "If the user refers to earlier work and the needed context is missing, ask them to restate or paste the relevant context instead of guessing. "
    "Tool output is clipped to keep long tasks inside model context. Most tool results are capped around 16k characters, bash and unified patch output keep both the start and end when clipped, and webfetch defaults to about 20k characters after HTML-to-markdown compaction. "
    "When clipped output is not enough, narrow the request and keep going with read offsets, grep, glob, list, or follow-up webfetch calls instead of guessing. "
    "Keep normal answers concise, but stay on task until you either finish the work or need human input; aim to complete as much useful work as possible in this session. "
    "Prefer patch for larger coordinated edits or renames; it accepts standard unified diffs and a friendlier file-oriented format starting with '*** Begin Patch'."
)
INTERACTIVE_SYSTEM_PROMPT = (
    "This run is interactive. Use the ask tool for plans, reviews, ambiguity, meaningful checkpoints, tradeoffs, and collaborative iteration when a short back-and-forth will improve the result. "
    "When the user asks for a plan, review, or multi-step collaboration, inspect the relevant code, produce a short plan, then use the ask tool to ask whether the plan is good or what should change if stdin is interactive. "
    "If the user gives feedback, revise and continue; for longer tasks, it is good to work in 2-3 meaningful batches and use ask between batches when the user wants review checkpoints. "
    "Do not ask after every trivial step, but do use ask for ambiguous product decisions, plan approval, tradeoffs, collaborative iteration, or when a quick checkpoint would help keep momentum. "
    "If the task looks likely to require many coordinated edits, a risky refactor, or other broad changes, ask before starting whether the user wants a short summary and commit of the current state so undo is easy. "
    "If you made changes and are about to finish with a normal status or completion summary, ask 'Would you like me to summarise and commit the work completed?' when stdin is interactive. "
    "If ask is unavailable, mention the same checkpoint or commit option in your normal response when it would be useful. "
)
NONINTERACTIVE_SYSTEM_PROMPT = (
    "This run is non-interactive. The ask tool is unavailable, so do not pause for approvals, checkpoint questions, or conversational handoffs. "
    "Focus on completing the prompted work without interruptions, using the workspace and tool results to resolve uncertainty whenever possible. "
    "Choose reasonable, safe defaults when the repo and prompt provide enough direction, and keep going unless the task is blocked by missing credentials, an irreversible decision, or ambiguity that cannot be resolved from available context. "
    "Be resilient to recoverable faults: narrow the scope, inspect more, retry with adjusted commands, switch tools, or take a simpler path instead of stopping at the first failure. "
    "If you do end up blocked, give a concise completion-style status that explains what you tried, what remains blocked, and the smallest useful next step for the operator. "
)
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
_using_bedrock = False
_last_api_env_error = None
STDOUT = Console()
STDERR = Console(stderr=True)
HTML_MARKERS = ("text/html", "application/xhtml+xml")


def markdown(text="", *, stderr=False):
    console = STDERR if stderr else STDOUT
    if text:
        console.print(Markdown(str(text)))
    else:
        console.print()


def code_block(text, language="text"):
    body = str(text).rstrip("\n")
    return f"```{language}\n{body}\n```"


def inline_code(text):
    value = str(text).replace("`", "\\`")
    return f"`{value}`"


def eprint(text=""):
    markdown(text, stderr=True)


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
    lowered = (content_type or "").lower()
    if any(marker in lowered for marker in HTML_MARKERS):
        return True
    probe = text.lstrip()[:500].lower()
    return (
        probe.startswith("<!doctype html")
        or probe.startswith("<html")
        or ("<body" in probe and "<p" in probe)
    )


def webfetch_body(text, content_type):
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


def show(text, lines=2):
    snippet = "\n".join(line[:120] for line in text.splitlines()[: max(lines, 0)])
    if snippet:
        markdown(code_block(snippet), stderr=True)


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
    profile = env.get("AWS_PROFILE")
    status(
        "Refreshing AWS SSO session"
        + (f" for profile {inline_code(profile)}" if profile else "")
        + " with device-code login."
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
    missing = [tool for tool in ("bash", "patch") if not which(tool, env.get("PATH"))]
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
    path = (root / raw).resolve()
    return path if path == root or root in path.parents else root / Path(raw).name


def restore_text(lines, trailing_newline):
    text = "\n".join(lines)
    if trailing_newline and (lines or text == ""):
        text += "\n"
    return text


def find_subsequence(lines, needle, start=0):
    if not needle:
        return start
    end = len(lines) - len(needle) + 1
    for index in range(max(start, 0), max(end, 0)):
        if lines[index : index + len(needle)] == needle:
            return index
    return -1


def locate_insertion_point(lines, locator, start=0):
    marker = (locator or "").strip()
    if not marker:
        return min(max(start, 0), len(lines))
    for index in range(max(start, 0), len(lines)):
        if marker in lines[index]:
            return index + 1
    for index, line in enumerate(lines):
        if marker in line:
            return index + 1
    return min(max(start, 0), len(lines))


def parse_friendly_patch(patch_text):
    lines = patch_text.strip("\n").splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("friendly patch must start with '*** Begin Patch'")
    operations, index, ended = [], 1, False
    while index < len(lines):
        line = lines[index]
        if line.strip() == "*** End Patch":
            ended = True
            index += 1
            break
        if not line.strip():
            index += 1
            continue
        if line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            index += 1
            body = []
            while index < len(lines) and not lines[index].startswith("*** "):
                body.append(lines[index])
                index += 1
            operations.append(("add", path, None, body))
            continue
        if line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            operations.append(("delete", path, None, []))
            index += 1
            continue
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            index += 1
            move_to = None
            if index < len(lines) and lines[index].startswith("*** Move to: "):
                move_to = lines[index].removeprefix("*** Move to: ").strip()
                index += 1
            body = []
            while index < len(lines) and not lines[index].startswith("*** "):
                body.append(lines[index])
                index += 1
            operations.append(("update", path, move_to, body))
            continue
        raise ValueError(f"unknown friendly patch header: {line}")
    if not ended:
        raise ValueError("friendly patch must end with '*** End Patch'")
    if any(line.strip() for line in lines[index:]):
        raise ValueError("unexpected content after '*** End Patch'")
    return operations


def render_added_file(body_lines):
    content = []
    for line in body_lines:
        if not line.startswith("+"):
            raise ValueError("added file content lines must start with '+'")
        content.append(line[1:])
    return "\n".join(content)


def parse_update_hunks(body_lines):
    if not body_lines:
        return []
    hunks, locator, chunk = [], "", []
    for line in body_lines:
        if line.startswith("@@"):
            if chunk:
                hunks.append((locator, chunk))
                chunk = []
            locator = line[2:].strip()
            continue
        if line[:1] not in {" ", "+", "-"}:
            raise ValueError(
                "update patch lines must start with '@@', ' ', '+', or '-'"
            )
        chunk.append(line)
    if chunk:
        hunks.append((locator, chunk))
    return hunks


def apply_update_patch(original_text, body_lines):
    hunks = parse_update_hunks(body_lines)
    if not hunks:
        return original_text
    lines = original_text.splitlines()
    trailing_newline = original_text.endswith("\n")
    cursor = 0
    for locator, hunk_lines in hunks:
        old_lines, new_lines = [], []
        for line in hunk_lines:
            prefix, content = line[:1], line[1:]
            if prefix in {" ", "-"}:
                old_lines.append(content)
            if prefix in {" ", "+"}:
                new_lines.append(content)
        if old_lines:
            start = find_subsequence(lines, old_lines, cursor)
            if start < 0:
                start = find_subsequence(lines, old_lines, 0)
            if start < 0:
                raise ValueError(
                    "could not match patch hunk"
                    + (f" near {inline_code(locator)}" if locator else "")
                )
            lines[start : start + len(old_lines)] = new_lines
            cursor = start + len(new_lines)
            continue
        start = locate_insertion_point(lines, locator, cursor)
        lines[start:start] = new_lines
        cursor = start + len(new_lines)
    return restore_text(lines, trailing_newline)


def apply_friendly_patch(root, patch_text):
    summaries = []
    for kind, path, move_to, body_lines in parse_friendly_patch(patch_text):
        target = resolve_path(root, path)
        if kind == "add":
            if target.exists():
                raise ValueError(f"file already exists: {rel(root, target)}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_added_file(body_lines), encoding="utf-8")
            summaries.append(f"added {rel(root, target)}")
            continue
        if kind == "delete":
            if not target.exists():
                raise ValueError(f"file does not exist: {rel(root, target)}")
            if target.is_dir():
                raise ValueError(
                    f"cannot delete directory with patch: {rel(root, target)}"
                )
            target.unlink()
            summaries.append(f"deleted {rel(root, target)}")
            continue
        if not target.exists():
            raise ValueError(f"file does not exist: {rel(root, target)}")
        if target.is_dir():
            raise ValueError(f"cannot update directory with patch: {rel(root, target)}")
        updated_text = apply_update_patch(
            target.read_text(encoding="utf-8", errors="replace"), body_lines
        )
        destination = target if move_to is None else resolve_path(root, move_to)
        if destination != target and destination.exists():
            raise ValueError(f"destination already exists: {rel(root, destination)}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(updated_text, encoding="utf-8")
        if destination != target:
            target.unlink()
            summaries.append(f"updated {rel(root, target)} -> {rel(root, destination)}")
        else:
            summaries.append(f"updated {rel(root, target)}")
    return "\n".join(summaries)


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
    status(f"tool {inline_code(name)}" + (f": {detail_text}" if detail_text else ""))


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
    friendly = patch_text.lstrip().startswith("*** Begin Patch")
    if friendly:
        out = apply_friendly_patch(state["root"], patch_text)
        show(out, 3)
        return out
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
        f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip(),
        tail_chars=MAX_TOOL_OUTPUT_TAIL_CHARS,
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
        f"exit_code: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}".strip(),
        tail_chars=MAX_TOOL_OUTPUT_TAIL_CHARS,
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


def tool_webfetch(state, url, max_chars=MAX_WEBFETCH_CHARS):
    note_tool(state, "webfetch", url=url, max_chars=max_chars)
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("webfetch only supports http and https")
    status("Fetching web content.")
    with httpx.Client(follow_redirects=True, timeout=20.0) as http:
        response = http.get(parsed.geturl())
        response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    body = webfetch_body(response.text, content_type)
    out = (
        f"url: {response.url}\n"
        f"status: {response.status_code}\n"
        f"content-type: {content_type}\n"
        + ("content-format: markdown\n" if body != response.text else "")
        + f"\n{body}"
    )
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
    "write": (
        tool_write,
        "Create or overwrite a workspace file. Prefer edit for exact replacements and patch for larger coordinated changes.",
        {"path": STR, "content": STR},
        ["path", "content"],
    ),
    "edit": (
        tool_edit,
        "Replace exact text in an existing workspace file. Use this when you know the current text to swap.",
        {"path": STR, "old_text": STR, "new_text": STR, "replace_all": BOOL},
        ["path", "old_text", "new_text"],
    ),
    "patch": (
        tool_patch,
        "Apply coordinated file edits inside the workspace. Accepts standard unified diffs and a friendlier file-oriented format starting with '*** Begin Patch'.",
        {"patch_text": STR},
        ["patch_text"],
    ),
    "list": (
        tool_list,
        "List files and directories in a workspace directory. Use this to inspect unfamiliar paths before reading or writing.",
        {"path": STR, "limit": INT},
        [],
    ),
    "bash": (
        tool_bash,
        "Run shell commands for builds, tests, git, or package managers. Avoid using bash for routine file reading, editing, or search.",
        {"command": STR, "timeout_seconds": INT},
        ["command"],
    ),
    "read": (
        tool_read,
        "Read a workspace file or directory listing with optional offsets. Inspect with this before editing and for follow-up slices of large files.",
        {"path": STR, "offset": INT, "limit": INT},
        ["path"],
    ),
    "grep": (
        tool_grep,
        "Search workspace file contents with ripgrep or grep. Use this to find code by text or regex before reading specific files.",
        {"pattern": STR, "path": STR, "file_glob": STR},
        ["pattern"],
    ),
    "glob": (
        tool_glob,
        "Find files or directories by name pattern. Use this for discovery when you know the path shape but not the exact file.",
        {"pattern": STR, "path": STR},
        ["pattern"],
    ),
    "webfetch": (
        tool_webfetch,
        "Fetch a web page over HTTP or HTTPS. HTML pages are converted to markdown before truncation, with a default budget of about 20k chars.",
        {"url": STR, "max_chars": INT},
        ["url"],
    ),
    "ask": (
        tool_ask,
        "Ask the user a question and return their response. Use this for ambiguity, plan approval, review checkpoints, and summary or commit offers before or after substantial work.",
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


def responses_tools(tool_specs):
    return [
        {
            "type": "function",
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
            "strict": False,
        }
        for name, (_, desc, props, required) in tool_specs.items()
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
    tool_specs = state.get("tool_specs", TOOL_SPECS)
    if name not in tool_specs:
        return f"Error: Tool '{name}' is unavailable in this run"
    try:
        return str(tool_specs[name][0](state, **tool_args))
    except Exception as exc:
        return f"Error in {name}: {type(exc).__name__}: {exc}"


def list_model_ids():
    require_api_env()
    status("Loading available models.")
    return sorted(model.id for model in list(cast(OpenAI, get_client()).models.list()))


async def run_turn(
    client,
    kind,
    session,
    state,
    model,
    system_prompt,
    chat_tool_defs,
    responses_tool_defs,
    max_steps,
    allow_fallback=False,
):
    for _ in range(max_steps):
        status(f"Waiting for {inline_code(model)}.")
        if kind == "chat":
            response = await cast(AsyncOpenAI, client).chat.completions.create(
                model=model,
                messages=session,
                tools=chat_tool_defs,
                tool_choice="auto",
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
                    tools=responses_tool_defs,
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
    chat_tool_defs = chat_tools(tool_specs)
    responses_tool_defs = responses_tools(tool_specs)
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
                model,
                system_prompt,
                chat_tool_defs,
                responses_tool_defs,
                max_steps,
            )
        if mode == "always":
            return await run_turn(
                client,
                "responses",
                responses,
                state,
                model,
                system_prompt,
                chat_tool_defs,
                responses_tool_defs,
                max_steps,
            )
        try:
            return await run_turn(
                client,
                "responses",
                responses,
                state,
                model,
                system_prompt,
                chat_tool_defs,
                responses_tool_defs,
                max_steps,
                True,
            )
        except ResponsesFallback as exc:
            warning(
                f"Responses API unavailable ({exc.reason}). Falling back to Chat Completions."
            )
            return await run_turn(
                client,
                "chat",
                chat,
                state,
                model,
                system_prompt,
                chat_tool_defs,
                responses_tool_defs,
                max_steps,
            )

    try:
        return await run_mode(get_client(async_=True))
    except (AuthenticationError, PermissionDeniedError) as exc:
        if ensure_api_env(root, refresh=True):
            warning("Bedrock token expired. Refreshing credentials.")
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
    commands = {"run", "bedrock-token", "models", "model", "-h", "--help"}
    if not args:
        args = ["run"] if not sys.stdin.isatty() else ["--help"]
    elif args[0] in {"-v", "--version"}:
        render_markdown(f"oy {__version__}")
        return 0
    elif args[0] not in commands:
        args = ["run", *args]
    result = defopt.run(
        [run, bedrock_token, models, model], argv=args, version=False, short={}
    )
    return 0 if result is None else result


if __name__ == "__main__":
    raise SystemExit(main())

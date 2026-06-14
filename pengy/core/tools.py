"""Tool definitions and execution for Pengy."""
import concurrent.futures
import fnmatch
import os
import re
import signal
import subprocess
import sys as _sys_module
import tempfile
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from ddgs import DDGS

# Set by the UI layer so _run_bash can request a sudo password interactively.
_sudo_password_provider = None
# Cache the sudo password after first prompt so we don't ask repeatedly.
_cached_sudo_password = None

_user_agent = "PengyAgent/1.0"

_tool_timeout = 60  # seconds; -1 means no timeout

# Read-only tools that can be auto-approved when tool_confirmation is "safe"
READONLY_TOOLS = frozenset({
    "read_file", "read_multiple_files", "directory_tree",
    "search_content", "web_search", "fetch_url",
})

# Active subprocess handle so the UI layer can kill runaway tools
_active_process = None
_active_process_lock = threading.Lock()

# Maximum download size for download_file (100 MB)
_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024

# Directories/files to skip in directory_tree and search_content
_ALWAYS_SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", ".DS_Store",
    "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".eggs",
}
_ALWAYS_SKIP_SUFFIXES = (".egg-info",)
_ALWAYS_SKIP_FILES = {".DS_Store", "Thumbs.db"}


def set_sudo_password_provider(fn):
    global _sudo_password_provider, _cached_sudo_password
    _sudo_password_provider = fn
    if fn is None:
        # Session ended — clear cached password for safety
        _cached_sudo_password = None


def set_user_agent(ua: str):
    global _user_agent
    _user_agent = ua or "PengyAgent/1.0"


def set_tool_timeout(seconds: int):
    """Set the timeout for tool calls.  -1 means no timeout."""
    global _tool_timeout
    _tool_timeout = seconds

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The file path to read",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The file path to write to",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Perform an exact string replacement in an existing file. The old_str must match exactly one occurrence in the file — if zero or multiple matches are found, the edit is rejected with a clear error. This is the preferred way to make targeted edits instead of rewriting an entire file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The file path to edit",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "The exact text to find and replace. Must match exactly one location in the file, including whitespace and indentation.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "The text to replace it with. Use an empty string to delete.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a bash command in the terminal",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 5)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "Download a file from a URL to the user's Downloads directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the file to download",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename to save as; defaults to the name from the URL",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch the text content of a URL into the context window, useful for reading documentation or web pages before coding",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute Python code",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python code to execute",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "directory_tree",
            "description": "Show a visual tree of the directory structure, useful for understanding project layout quickly. Skips common noise directories like .git, node_modules, __pycache__ by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "The directory path to show the tree for",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum depth to recurse (default: 3)",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "Whether to show hidden files/directories starting with '.' (default: false)",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_multiple_files",
            "description": "Read multiple files at once, returning each with a clear header. Use this when you know you need to inspect several files to reduce round-trips.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to read",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": "Search for a regex pattern in files under a directory. Returns matching lines with file path, line number, and optional surrounding context. Useful for finding where symbols, strings, or patterns appear in a codebase. Skips binary files and common noise directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The regex pattern to search for. Use simple strings for literal matching — they will be escaped automatically. Use proper regex syntax for patterns, e.g. r'class\\s+\\w+'",
                    },
                    "path": {
                        "type": "string",
                        "description": "The directory or file to search in",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "Optional glob to filter files, e.g. '*.py' or '*.{js,ts}'. Defaults to all text files.",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Number of lines of context to show before and after each match (default: 0)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matches to return (default: 50)",
                    },
                },
                "required": ["pattern", "path"],
            },
        },
    },
]


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool and return the result."""
    if name == "read_file":
        return _read_file(arguments["path"])
    elif name == "write_file":
        return _write_file(arguments["path"], arguments["content"])
    elif name == "replace_in_file":
        return _replace_in_file(arguments["path"], arguments["old_str"], arguments["new_str"])
    elif name == "run_bash":
        return _run_bash(arguments["command"])
    elif name == "web_search":
        return _web_search(arguments["query"], arguments.get("max_results", 5))
    elif name == "download_file":
        return _download_file(arguments["url"], arguments.get("filename"))
    elif name == "fetch_url":
        return _fetch_url(arguments["url"])
    elif name == "run_python":
        return _run_python(arguments["code"])
    elif name == "directory_tree":
        return _directory_tree(
            arguments["path"],
            arguments.get("max_depth", 3),
            arguments.get("show_hidden", False),
        )
    elif name == "read_multiple_files":
        return _read_multiple_files(arguments["paths"])
    elif name == "search_content":
        return _search_content(
            arguments["pattern"],
            arguments["path"],
            arguments.get("file_glob"),
            arguments.get("context_lines", 0),
            arguments.get("max_results", 50),
        )
    else:
        return f"Unknown tool: {name}"


def _read_file(path: str) -> str:
    """Read file contents."""
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"Error: File not found: {path}"
        if not p.is_file():
            return f"Error: Not a file: {path}"
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"


def _write_file(path: str, content: str) -> str:
    """Write content to file."""
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Successfully wrote to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# replace_in_file
# ---------------------------------------------------------------------------

def _replace_in_file(path: str, old_str: str, new_str: str) -> str:
    """Replace an exact string in a file.  old_str must match exactly once."""
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"Error: File not found: {path}"
        if not p.is_file():
            return f"Error: Not a file: {path}"

        if not old_str:
            return (
                "Error: old_str is empty. You must provide the exact text to replace. "
                "To insert text, include the surrounding lines that identify where to insert."
            )

        content = p.read_text(encoding="utf-8")

        count = content.count(old_str)
        if count == 0:
            return (
                f"Error: old_str not found in {path}.\n\n"
                f"Tip: read the file first to get the exact text. "
                f"Leading/trailing whitespace and indentation must match exactly."
            )
        elif count > 1:
            # Find precise line numbers for each occurrence
            pos = 0
            found_lines = []
            for idx in range(count):
                pos = content.index(old_str, pos)
                line_num = content[:pos].count("\n") + 1
                found_lines.append(line_num)
                pos += 1

            return (
                f"Error: old_str matches {count} locations in {path}.\n\n"
                f"Matches found on lines: {found_lines}\n\n"
                f"Make old_str longer or more specific so it matches exactly one location. "
                f"Include more surrounding context to disambiguate."
            )

        # Exactly one match — perform the replacement
        new_content = content.replace(old_str, new_str, 1)
        p.write_text(new_content, encoding="utf-8")

        # Report what we did
        old_line = content[:content.index(old_str)].count("\n") + 1
        old_lines_count = old_str.count("\n") + 1
        new_lines_count = new_str.count("\n") + 1

        # Show a snippet of the change
        def numbered(text: str, start: int) -> str:
            return "\n".join(
                f"{start + i:4d} │ {line}"
                for i, line in enumerate(text.split("\n"))
            )

        snippet_before = numbered(old_str, old_line)
        snippet_after = numbered(new_str, old_line)

        return (
            f"✅ Successfully replaced in {path}:\n"
            f"   Lines {old_line}–{old_line + old_lines_count - 1} "
            f"→ {old_lines_count} line(s) replaced with {new_lines_count} line(s)\n\n"
            f"Before:\n{snippet_before}\n\n"
            f"After:\n{snippet_after}"
        )

    except UnicodeDecodeError:
        return f"Error: {path} is a binary file (not UTF-8)."
    except Exception as e:
        return f"Error replacing text in file: {e}"


def _run_bash(command: str) -> str:
    """Run a bash command."""
    global _active_process
    proc = None
    try:
        stdin_input = None
        if re.search(r'\bsudo\b', command):
            if _sudo_password_provider is None:
                return "Error: sudo detected but no password provider is configured."
            global _cached_sudo_password
            password = _cached_sudo_password
            if password is None:
                password = _sudo_password_provider()
                if password is None:
                    return "Cancelled: sudo password not provided."
                _cached_sudo_password = password
            command = re.sub(r'\bsudo\b(?!\s+-S)', 'sudo -S', command, count=1)
            stdin_input = password + "\n"

        timeout = None if _tool_timeout == -1 else _tool_timeout
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE if stdin_input else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with _active_process_lock:
            _active_process = proc
        try:
            stdout, stderr = proc.communicate(input=stdin_input, timeout=timeout)
        finally:
            with _active_process_lock:
                if _active_process is proc:
                    _active_process = None

        output = stdout or ""
        if stdin_input:
            stderr = re.sub(r'^\[sudo[^]]*\].*\n?', '', stderr or "", flags=re.MULTILINE).strip()
        if stderr:
            output += "\n" + stderr
        if proc.returncode != 0:
            output += f"\n[Exit code: {proc.returncode}]"
        return output or "(No output)"
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        with _active_process_lock:
            if _active_process is proc:
                _active_process = None
        return f"Error: Command timed out after {_tool_timeout} seconds"
    except Exception as e:
        with _active_process_lock:
            if _active_process is proc:
                _active_process = None
        return f"Error running command: {e}"


def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo."""
    def _do_search():
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    # shutdown(wait=False) is critical: if result(timeout=5) raises TimeoutError,
    # the context-manager form would call shutdown(wait=True) and hang forever on
    # the still-running thread.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        results = executor.submit(_do_search).result(timeout=5)
    except concurrent.futures.TimeoutError:
        return "Web search timed out after 5 seconds. Please try again."
    except Exception as e:
        return f"Error performing web search: {e}"
    finally:
        executor.shutdown(wait=False)

    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}")
        lines.append(f"   URL: {r.get('href', '')}")
        lines.append(f"   {r.get('body', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def _download_file(url: str, filename: str | None = None) -> str:
    """Download a file to ~/Downloads/."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Error: Only http/https URLs are supported (got '{parsed.scheme}')."

        downloads = Path.home() / "Downloads"
        downloads.mkdir(exist_ok=True)
        if not filename:
            filename = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
        dest = downloads / filename
        req = urllib.request.Request(url, headers={"User-Agent": _user_agent})
        timeout = None if _tool_timeout == -1 else _tool_timeout
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Stream to disk in chunks, enforcing a size cap
            total = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_SIZE:
                        out.close()
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                        return (
                            f"Error: Download exceeds maximum size of "
                            f"{_MAX_DOWNLOAD_SIZE:,} bytes."
                        )
                    out.write(chunk)
        return f"Downloaded to {dest} ({total:,} bytes)"
    except Exception as e:
        return f"Error downloading file: {e}"


class _TextExtractor(HTMLParser):
    """Strip HTML tags and collect visible text."""
    def __init__(self):
        super().__init__()
        self._parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = False
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _fetch_url(url: str) -> str:
    """Fetch a URL and return its text content."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Error: Only http/https URLs are supported (got '{parsed.scheme}')."

        req = urllib.request.Request(url, headers={"User-Agent": _user_agent})
        with urllib.request.urlopen(req, timeout=None if _tool_timeout == -1 else _tool_timeout) as resp:
            content_type = resp.headers.get_content_type()
            raw = resp.read(2 * 1024 * 1024)  # cap at 2 MB
        text = raw.decode("utf-8", errors="replace")
        if "html" in content_type:
            parser = _TextExtractor()
            parser.feed(text)
            text = parser.get_text()
        # Truncate to ~50k chars so it fits comfortably in context
        if len(text) > 50_000:
            text = text[:50_000] + "\n\n[... truncated at 50,000 characters ...]"
        return text
    except Exception as e:
        return f"Error fetching URL: {e}"


def _run_python(code: str) -> str:
    """Execute Python code."""
    global _active_process
    proc = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(code)
            temp_file = f.name
        timeout = None if _tool_timeout == -1 else _tool_timeout
        proc = subprocess.Popen(
            [_sys_module.executable, temp_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with _active_process_lock:
            _active_process = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        finally:
            with _active_process_lock:
                if _active_process is proc:
                    _active_process = None

        output = stdout or ""
        if stderr:
            output += "\n" + stderr
        if proc.returncode != 0:
            output += f"\n[Exit code: {proc.returncode}]"
        return output or "(No output)"
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        with _active_process_lock:
            if _active_process is proc:
                _active_process = None
        return f"Error: Python execution timed out after {_tool_timeout} seconds"
    except Exception as e:
        with _active_process_lock:
            if _active_process is proc:
                _active_process = None
        return f"Error running Python: {e}"


# ---------------------------------------------------------------------------
# directory_tree
# ---------------------------------------------------------------------------

def _should_skip_tree(name: str, show_hidden: bool) -> bool:
    """Return True if this entry should be skipped in a directory tree listing."""
    if not show_hidden and name.startswith("."):
        return True
    if name in _ALWAYS_SKIP_DIRS:
        return True
    if name.endswith(_ALWAYS_SKIP_SUFFIXES):
        return True
    return False


def _build_tree(root: Path, prefix: str, depth: int, max_depth: int,
                show_hidden: bool, lines: list, file_count: list,
                max_entries: int = 500) -> None:
    """Recursively build tree lines.  file_count is a single-element list
    used as a mutable counter across calls."""
    if depth > max_depth:
        return
    if file_count[0] >= max_entries:
        return

    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        lines.append(f"{prefix}[Permission denied]")
        return
    except OSError as e:
        lines.append(f"{prefix}[Error: {e}]")
        return

    # Filter out skippable entries
    entries = [e for e in entries if not _should_skip_tree(e.name, show_hidden)]

    for i, entry in enumerate(entries):
        if file_count[0] >= max_entries:
            lines.append(f"{prefix}... (truncated, {max_entries} entries reached)")
            return

        is_last = (i == len(entries) - 1)
        connector = "└── " if is_last else "├── "
        is_dir = entry.is_dir()

        if is_dir:
            lines.append(f"{prefix}{connector}{entry.name}/")
            file_count[0] += 1
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{prefix}{connector}{entry.name}  ({_format_size(size)})")
            file_count[0] += 1

        if is_dir and depth < max_depth:
            extension = "    " if is_last else "│   "
            _build_tree(entry, prefix + extension, depth + 1, max_depth,
                        show_hidden, lines, file_count, max_entries)


def _format_size(size: int) -> str:
    """Format a file size for human consumption."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"


def _directory_tree(path: str, max_depth: int = 3, show_hidden: bool = False) -> str:
    """Produce a visual directory tree."""
    try:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return f"Error: Directory not found: {path}"
        if not root.is_dir():
            return f"Error: Not a directory: {path}"

        lines = [str(root) + "/"]
        file_count = [0]
        _build_tree(root, "", 1, max_depth, show_hidden, lines, file_count)

        if not lines[1:]:
            lines.append("(empty directory)")

        result = "\n".join(lines)
        if len(result) > 40_000:
            result = result[:40_000] + "\n\n[... truncated at 40,000 characters ...]"
        return result
    except Exception as e:
        return f"Error building directory tree: {e}"


# ---------------------------------------------------------------------------
# read_multiple_files
# ---------------------------------------------------------------------------

_MAX_FILES = 20
_MAX_PER_FILE = 50_000
_MAX_TOTAL = 120_000


def _read_multiple_files(paths: list[str]) -> str:
    """Read multiple files, returning each under a clear header."""
    if not paths:
        return "Error: no paths provided."
    if len(paths) > _MAX_FILES:
        return (
            f"Error: too many files ({len(paths)}). "
            f"Maximum is {_MAX_FILES}. Please narrow your selection."
        )

    parts: list[str] = []
    total_chars = 0
    errors = 0

    for i, raw_path in enumerate(paths):
        p = Path(raw_path).expanduser()
        header = f"{'='*60}\n📄 {raw_path}"

        if not p.exists():
            parts.append(f"{header}\n  ❌ File not found.")
            errors += 1
            continue
        if not p.is_file():
            parts.append(f"{header}\n  ❌ Not a file.")
            errors += 1
            continue

        try:
            content = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            parts.append(f"{header}\n  ❌ Binary file (not UTF-8).")
            errors += 1
            continue
        except Exception as e:
            parts.append(f"{header}\n  ❌ Error reading file: {e}")
            errors += 1
            continue

        # Truncate individual file if needed
        if len(content) > _MAX_PER_FILE:
            content = content[:_MAX_PER_FILE] + (
                f"\n\n[... truncated at {_MAX_PER_FILE:,} characters "
                f"(full file is {p.stat().st_size:,} bytes) ...]"
            )

        # Check if adding this file would blow the total budget
        block = f"{header}\n{content}"
        if total_chars + len(block) > _MAX_TOTAL:
            remaining = _MAX_TOTAL - total_chars
            if remaining > 200:
                block = block[:remaining] + "\n[... truncated to fit total output limit ...]"
            else:
                parts.append(f"\n[... output limit reached; {len(paths) - i} files skipped ...]")
                break

        parts.append(block)
        total_chars += len(block)

    if errors == len(paths):
        return "\n\n".join(parts)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# search_content
# ---------------------------------------------------------------------------

# Common text file extensions to limit searches
_TEXT_EXTENSIONS = {
    ".py", ".pyi", ".pyx", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".rs", ".go", ".java", ".kt", ".scala", ".swift",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rb", ".rake", ".php", ".pl", ".pm",
    ".sh", ".bash", ".zsh", ".fish",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".svg", ".rss",
    ".md", ".markdown", ".rst", ".txt", ".tex",
    ".sql", ".r", ".jl", ".lua", ".zig", ".nim", ".ex", ".exs",
    ".cmake", ".make", ".mk", ".dockerfile", ".env",
    ".gitignore", ".editorconfig",
}


def _is_likely_text(entry: Path) -> bool:
    """Guess whether a file is a text file based on extension and name."""
    if entry.suffix.lower() in _TEXT_EXTENSIONS:
        return True
    # Also match files with no extension that are commonly text
    name = entry.name.lower()
    if name in {"makefile", "dockerfile", "license", "changelog", "authors", "todo"}:
        return True
    return False


def _should_skip_search_dir(dirname: str) -> bool:
    """Return True if this directory should be skipped during search."""
    if dirname.startswith("."):
        return True
    if dirname in _ALWAYS_SKIP_DIRS:
        return True
    if dirname.endswith(_ALWAYS_SKIP_SUFFIXES):
        return True
    return False


def _matches_glob(name: str, glob: str | None) -> bool:
    """Check if a filename matches the given glob pattern."""
    if glob is None:
        return True
    # Support brace expansion like *.{js,ts}
    if "{" in glob and "}" in glob:
        patterns = _expand_braces(glob)
        return any(fnmatch.fnmatch(name, p) for p in patterns)
    return fnmatch.fnmatch(name, glob)


def _expand_braces(pattern: str) -> list[str]:
    """Expand simple brace patterns like '*.{py,js}' into ['*.py', '*.js']."""
    m = re.match(r'^(.*)\{([^}]+)\}(.*)$', pattern)
    if not m:
        return [pattern]
    prefix, choices, suffix = m.groups()
    return [prefix + c.strip() + suffix for c in choices.split(",")]


def _search_content(pattern: str, path: str, file_glob: str | None,
                    context_lines: int, max_results: int) -> str:
    """Search for a regex pattern in files."""
    try:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            return f"Error: Path not found: {path}"
    except Exception as e:
        return f"Error resolving path: {e}"

    # Compile the regex
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        # Try escaping as a literal
        try:
            compiled = re.compile(re.escape(pattern))
        except re.error:
            return f"Error: Invalid regex pattern: {e}"

    context_lines = max(0, min(context_lines, 10))
    max_results = max(1, min(max_results, 200))

    results: list[str] = []
    files_searched = 0
    files_skipped = 0
    truncated = False

    # If the path is a single file, search just that file
    if root.is_file():
        _search_one_file(root, compiled, context_lines, results,
                         max_results, None)
        if not results:
            return f"No matches found for '{pattern}' in {path}"
        return "\n\n".join(results)

    # Walk the directory
    try:
        for dirpath_str, dirnames, filenames in os.walk(root):
            # Filter and sort dirnames in-place for reproducibility
            dirnames[:] = sorted(
                [d for d in dirnames if not _should_skip_search_dir(d)]
            )
            filenames.sort()

            current_dir = Path(dirpath_str)

            for fname in filenames:
                if fname in _ALWAYS_SKIP_FILES:
                    continue
                if not _matches_glob(fname, file_glob):
                    continue

                filepath = current_dir / fname
                if not _is_likely_text(filepath):
                    files_skipped += 1
                    continue

                files_searched += 1
                hit_limit = _search_one_file(
                    filepath, compiled, context_lines, results,
                    max_results, root,
                )
                if hit_limit:
                    truncated = True
                    break

            if truncated:
                break

    except PermissionError:
        pass
    except OSError:
        pass

    if not results:
        summary = f"No matches found for '{pattern}' in {path}"
        if files_searched:
            summary += f" (searched {files_searched} files"
            if files_skipped:
                summary += f", skipped {files_skipped} binary/non-matching files"
            summary += ")"
        return summary

    out = "\n\n".join(results)
    summary = (
        f"Found {len(results)} match(es) for '{pattern}' "
        f"across {files_searched} file(s)"
    )
    if truncated:
        summary += " (results truncated)"
    return summary + "\n" + "─" * 60 + "\n" + out


def _search_one_file(filepath: Path, compiled: "re.Pattern",
                     context_lines: int, results: list,
                     max_results: int, root: Path | None) -> bool:
    """Search a single file and append matches to results.  Returns True if
    the result limit was hit (caller should stop searching more files)."""
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except (UnicodeDecodeError, OSError):
        return False

    lines = content.split("\n")
    # Pre-compute matched line indices
    matched_lines: set[int] = set()
    for i, line in enumerate(lines):
        if compiled.search(line):
            matched_lines.add(i)

    if not matched_lines:
        return False

    # Build display path (relative to root if possible)
    if root:
        try:
            display = str(filepath.relative_to(root))
        except ValueError:
            display = str(filepath)
    else:
        display = str(filepath)

    # Group contiguous match regions so context doesn't duplicate
    regions = _group_regions(matched_lines, context_lines, len(lines))

    blocks: list[str] = []
    for start, end in regions:
        if len(results) >= max_results:
            return True

        block_lines = [f"📄 {display}:"]
        for ln in range(start, end):
            marker = " ▸" if ln in matched_lines else "  "
            # Show the line with its original 1-based number
            block_lines.append(f"{marker}{ln + 1:5d} │ {lines[ln]}")
        blocks.append("\n".join(block_lines))

        results.append("\n".join(block_lines))

        if len(results) >= max_results:
            return True

    return False


def _group_regions(matched: set[int], context: int, total_lines: int
                   ) -> list[tuple[int, int]]:
    """Group matched line numbers into regions with context, merging overlaps."""
    regions: list[tuple[int, int]] = []
    for line in sorted(matched):
        start = max(0, line - context)
        end = min(total_lines, line + context + 1)
        if regions and start <= regions[-1][1]:
            # Merge overlapping region
            regions[-1] = (regions[-1][0], max(regions[-1][1], end))
        else:
            regions.append((start, end))
    return regions


# ---------------------------------------------------------------------------
# process management helpers
# ---------------------------------------------------------------------------

def kill_active_process():
    """Kill the currently active tool subprocess, if any.

    Called by the UI layer on Stop / cancel.  Uses SIGKILL on the entire
    process group so that any child processes are also terminated.
    """
    proc = None
    with _active_process_lock:
        proc = _active_process
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def is_readonly_tool(name: str) -> bool:
    """Return True if the named tool is read-only (no side effects)."""
    return name in READONLY_TOOLS

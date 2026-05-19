"""Tool definitions and execution for Pengy."""
import concurrent.futures
import re
import subprocess
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from ddgs import DDGS

# Set by the UI layer so _run_bash can request a sudo password interactively.
_sudo_password_provider = None


def set_sudo_password_provider(fn):
    global _sudo_password_provider
    _sudo_password_provider = fn

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
]


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool and return the result."""
    if name == "read_file":
        return _read_file(arguments["path"])
    elif name == "write_file":
        return _write_file(arguments["path"], arguments["content"])
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


def _run_bash(command: str) -> str:
    """Run a bash command."""
    try:
        stdin_input = None
        if re.search(r'\bsudo\b', command):
            if _sudo_password_provider is None:
                return "Error: sudo detected but no password provider is configured."
            password = _sudo_password_provider()
            if password is None:
                return "Cancelled: sudo password not provided."
            # Inject -S so sudo reads password from stdin
            command = re.sub(r'\bsudo\b(?!\s+-S)', 'sudo -S', command, count=1)
            stdin_input = password + "\n"

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            input=stdin_input,
        )
        output = result.stdout
        stderr = result.stderr
        if stdin_input:
            # Strip the sudo password prompt line from stderr
            stderr = re.sub(r'^\[sudo\].*\n?', '', stderr, flags=re.MULTILINE).strip()
        if stderr:
            output += "\n" + stderr
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
        return output or "(No output)"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 60 seconds"
    except Exception as e:
        return f"Error running command: {e}"


def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo."""
    def _do_search():
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            results = executor.submit(_do_search).result(timeout=5)
        if not results:
            return "No results found."
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}")
            lines.append(f"   URL: {r.get('href', '')}")
            lines.append(f"   {r.get('body', '')}")
            lines.append("")
        return "\n".join(lines).strip()
    except concurrent.futures.TimeoutError:
        return "Web search timed out after 5 seconds. Please try again."
    except Exception as e:
        return f"Error performing web search: {e}"


def _download_file(url: str, filename: str | None = None) -> str:
    """Download a file to ~/Downloads/."""
    try:
        downloads = Path.home() / "Downloads"
        downloads.mkdir(exist_ok=True)
        if not filename:
            filename = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
        dest = downloads / filename
        urllib.request.urlretrieve(url, dest)
        size = dest.stat().st_size
        return f"Downloaded to {dest} ({size:,} bytes)"
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
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
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
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False
        ) as f:
            f.write(code)
            temp_file = f.name
        result = subprocess.run(
            ["python3", temp_file],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
        return output or "(No output)"
    except subprocess.TimeoutExpired:
        return "Error: Python execution timed out after 30 seconds"
    except Exception as e:
        return f"Error running Python: {e}"

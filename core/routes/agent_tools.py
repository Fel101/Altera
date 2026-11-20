"""Local agentic tool routes for the web UI.

These endpoints are reachable by anything that can talk to Altera's port, which on a desktop means every page open in the
browser and every other process on the machine. They are therefore scoped deliberately, not incidentally:

  * reading is limited to the project tree, minus anything that holds a secret (see SECRET_NAMES / SECRET_SUFFIXES);
  * writing is limited to WRITABLE_SUBDIR. The path-traversal guard alone was not enough: staying *inside* the project
    is exactly what an attacker wants, because dropping a .py file into core/routes/ runs it on the next restart;
  * fetching refuses to reach loopback, private or link-local addresses, so it cannot be used to read the model server,
    ComfyUI, NPC Forge or a cloud metadata endpoint on the caller's behalf;
  * anything that writes or reaches out refuses requests a browser made on behalf of another website.

Set tools.enabled: false in config.yaml to switch the whole surface off.
"""

from core.paths import DEFAULT_ROOT
from http.client import HTTPConnection, HTTPSConnection
import ipaddress
import os
import socket
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from flask import Blueprint, jsonify, request

bp = Blueprint("agent_tools", __name__)

PROJECT_ROOT = DEFAULT_ROOT
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 512 * 1024
MAX_FETCH_BYTES = 512 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_S = 8
IGNORED_DIRS = {".git", ".hermes", ".pytest_cache", ".venv", "__pycache__", "backup", "backups", "data"}

# Everything the UI may write goes here. It is created on demand and is the only writable location.
WRITABLE_SUBDIR = "workspace"

# Files that hold credentials or private data: never readable through the web UI, wherever they sit in the tree.
SECRET_NAMES = {"config.yaml", "config.local.yaml", "user_profile.yaml", "user_profile.local.yaml"}
SECRET_SUFFIXES = (".env", ".db", ".sqlite", ".sqlite3", ".key", ".pem", ".pfx", ".p12")


def _json_error(message, status=400):
    return jsonify({"error": message}), status


def _tools_enabled() -> bool:
    """Read the current permission; missing, malformed or unreadable config disables tools."""
    try:
        from core.config import get_config
        settings = get_config().get("tools")
        return isinstance(settings, dict) and settings.get("enabled") is True
    except Exception:
        return False


def _cross_site() -> bool:
    """True for a request that a browser made on behalf of another website."""
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return True
    origin = request.headers.get("Origin")
    return bool(origin) and urlparse(origin).netloc != request.host


def _guard():
    """The error response for a request that may not proceed, or None."""
    if not _tools_enabled():
        return _json_error("The workspace tools are switched off (tools.enabled in config.yaml).", 403)
    if _cross_site():
        return _json_error("Cross-site requests are not allowed.", 403)
    return None


def _is_hidden_or_private(path: Path) -> bool:
    try:
        rel_parts = path.relative_to(PROJECT_ROOT).parts
    except ValueError:
        return True
    return bool(rel_parts and rel_parts[0] in IGNORED_DIRS) or any(part.startswith(".") for part in rel_parts)


def _holds_a_secret(path: Path) -> bool:
    name = path.name.lower()
    return name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES)


def _resolve_workspace_path(raw_path, for_writing=False):
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path is required")

    normalized = raw_path.strip().replace("\\", os.sep).replace("/", os.sep)
    candidate = (PROJECT_ROOT / normalized).resolve()

    try:
        candidate.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("path must stay inside the project workspace") from exc

    if _is_hidden_or_private(candidate):
        raise ValueError("hidden/private paths are not available from the web UI")

    if for_writing:
        writable_root = (PROJECT_ROOT / WRITABLE_SUBDIR).resolve()
        try:
            candidate.relative_to(writable_root)
        except ValueError as exc:
            raise ValueError(f"the web UI can only write inside {WRITABLE_SUBDIR}/") from exc
    elif _holds_a_secret(candidate):
        raise ValueError("that file holds private data and is not readable from the web UI")

    return candidate


@bp.route("/api/tools/files", methods=["GET"])
def list_workspace_files():
    """List visible project files for the UI workspace browser."""
    denied = _guard()
    if denied:
        return denied

    files = []
    for root, dirs, filenames in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        root_path = Path(root)
        for filename in filenames:
            if filename.startswith("."):
                continue
            path = root_path / filename
            if _holds_a_secret(path):
                continue
            try:
                stat = path.stat()
                rel = path.relative_to(PROJECT_ROOT).as_posix()
            except OSError:
                continue
            files.append({
                "path": rel,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
            })

    files.sort(key=lambda item: item["path"].lower())
    return jsonify({"root": str(PROJECT_ROOT), "files": files[:500], "writable_dir": WRITABLE_SUBDIR})


@bp.route("/api/tools/file", methods=["GET"])
def read_workspace_file():
    """Read a text file inside the project workspace."""
    denied = _guard()
    if denied:
        return denied

    try:
        path = _resolve_workspace_path(request.args.get("path", ""))
    except ValueError as exc:
        return _json_error(str(exc))

    if not path.exists() or not path.is_file():
        return _json_error("file not found", 404)
    if path.stat().st_size > MAX_READ_BYTES:
        return _json_error("file is too large to read from the web UI", 413)

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _json_error("only UTF-8 text files can be read from the web UI", 415)
    except OSError:
        return _json_error("could not read file", 500)

    return jsonify({
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "content": content,
        "size": len(content.encode("utf-8")),
    })


@bp.route("/api/tools/file", methods=["POST"])
def write_workspace_file():
    """Write a UTF-8 text file inside the project's writable subdirectory."""
    denied = _guard()
    if denied:
        return denied

    data = request.json or {}
    try:
        path = _resolve_workspace_path(data.get("path", ""), for_writing=True)
    except ValueError as exc:
        return _json_error(str(exc))

    content = data.get("content", "")
    if not isinstance(content, str):
        return _json_error("content must be text")
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return _json_error("content is too large to write from the web UI", 413)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        return _json_error("could not write file", 500)

    return jsonify({
        "status": "ok",
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "size": len(content.encode("utf-8")),
    })


# ============================================================
# Fetching a URL, without becoming a way into this machine's own network
# ============================================================

def _public_address(value) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # is_global also excludes shared address space (100.64/10), including
    # Tailscale peers. Multicast is not a usable public HTTP destination.
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_loopback and not address.is_link_local and not address.is_unspecified)


def _is_reachable_address(host: str) -> bool:
    """False when a hostname resolves anywhere that is not the public internet.

    Without this, a page could ask Altera to read http://127.0.0.1:1234 (the model server), the ComfyUI or NPC Forge port,
    a router's admin page, or a cloud metadata endpoint, and hand back the response body. Every address the name
    resolves to has to pass, because one name can return several.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    return all(_public_address(info[4][0]) for info in infos)


def _public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once for this connection, validate every answer, and connect by IP.

    An earlier hostname check cannot protect a later DNS lookup: a name can
    change to loopback between validation and connection. HTTPConnection uses
    this function instead of socket.create_connection, while keeping its
    original hostname for the Host header and HTTPS certificate/SNI handling.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not infos or any(not _public_address(info[4][0]) for info in infos):
        raise OSError("The destination is not a public internet address.")
    last_error = None
    for family, socktype, proto, _, sockaddr in infos:
        connection = None
        try:
            connection = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as error:
            last_error = error
            if connection is not None:
                connection.close()
    raise last_error or OSError("No usable destination address.")


class _PublicHTTPConnection(HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


def _check_fetch_url(url: str):
    """(parsed_url, None) when the URL may be fetched, or (None, error_message)."""
    try:
        parsed = urlsplit(url)
        parsed.port  # noqa: B018 - Reject malformed/out-of-range ports before constructing a request.
    except ValueError:
        return None, "url must be a valid http or https URL"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "url must be http or https"
    if not _is_reachable_address(parsed.hostname or ""):
        return None, "that address is not reachable from the web UI"
    return parsed, None


class _GuardedRedirects(HTTPRedirectHandler):
    """Follow redirects, but check every hop: an open redirect to 127.0.0.1 would otherwise undo the check above."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _, error = _check_fetch_url(newurl)
        if error:
            raise URLError(f"redirect refused: {error}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@bp.route("/api/tools/fetch", methods=["POST"])
def fetch_url():
    """Fetch a small HTTP(S) resource for quick research from the UI."""
    denied = _guard()
    if denied:
        return denied

    data = request.json or {}
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        return _json_error("url must be text")
    url = data["url"].strip()
    _, error = _check_fetch_url(url)
    if error:
        return _json_error(error)

    req = Request(url, headers={"User-Agent": "Altera-Project-Local-UI/1.0"})
    # Do not inherit HTTP(S)_PROXY: connecting through an ambient proxy would
    # let that proxy resolve a previously checked hostname to a private address.
    opener = build_opener(ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(), _GuardedRedirects())
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT_S) as response:
            raw = response.read(MAX_FETCH_BYTES + 1)
            if len(raw) > MAX_FETCH_BYTES:
                return _json_error("response is too large to fetch from the web UI", 413)
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            return jsonify({
                "url": response.geturl(),
                "status": response.status,
                "content_type": response.headers.get("Content-Type", ""),
                "content": text,
                "truncated": False,
            })
    except HTTPError as exc:
        return _json_error(f"fetch failed with HTTP {exc.code}", exc.code if exc.code < 500 else 502)
    except (URLError, TimeoutError, OSError):
        return _json_error("fetch failed", 502)

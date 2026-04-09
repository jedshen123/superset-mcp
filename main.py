from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    AsyncIterator,
    Callable,
    TypeVar,
    Awaitable,
    Union,
)
import argparse
import base64
import contextvars
import os
import httpx
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
import inspect
from threading import Thread
import webbrowser
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from mcp.server.fastmcp import FastMCP, Context
from dotenv import load_dotenv
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

"""
Superset MCP Integration

This module provides a Model Control Protocol (MCP) server for Apache Superset,
enabling AI assistants to interact with and control a Superset instance programmatically.

It includes tools for:
- Authentication and token management
- Dashboard operations (list, get, create, update, delete)
- Chart management (list, get, create, update, delete)
- Database and dataset operations
- SQL execution and query management
- User information and recent activity tracking
- Advanced data type handling
- Tag management

Each tool follows a consistent naming convention: superset_<category>_<action>
"""

# Load environment variables from .env file
load_dotenv()

# Constants
SUPERSET_BASE_URL = os.getenv("SUPERSET_BASE_URL", "http://localhost:8088")
SUPERSET_USERNAME = os.getenv("SUPERSET_USERNAME")
SUPERSET_PASSWORD = os.getenv("SUPERSET_PASSWORD")
ACCESS_TOKEN_STORE_PATH = os.path.join(os.path.dirname(__file__), ".superset_token")

# Superset 6.0+ API endpoints (stable in 4.x–6.x)
SECURITY_LOGIN_ENDPOINT = "/api/v1/security/login"
SECURITY_REFRESH_ENDPOINT = "/api/v1/security/refresh"
SECURITY_CSRF_ENDPOINT = "/api/v1/security/csrf_token/"
ME_ENDPOINT = "/api/v1/me/"
# Auth provider: "db" = database (username/password). Required for Superset 6.0.
AUTH_PROVIDER = "db"

# Initialize FastAPI app for handling additional web endpoints if needed
app = FastAPI(title="Superset MCP Server")

# Request-scoped Superset context when using HTTP Basic Auth (per-user Superset credentials)
_current_request_superset_context: contextvars.ContextVar[Optional["SupersetContext"]] = (
    contextvars.ContextVar("current_request_superset_context", default=None)
)


@dataclass
class SupersetContext:
    """Typed context for the Superset MCP server"""

    client: httpx.AsyncClient
    base_url: str
    access_token: Optional[str] = None
    csrf_token: Optional[str] = None
    app: FastAPI = None


def load_stored_token() -> Optional[str]:
    """Load stored access token if it exists"""
    try:
        if os.path.exists(ACCESS_TOKEN_STORE_PATH):
            with open(ACCESS_TOKEN_STORE_PATH, "r") as f:
                return f.read().strip()
    except Exception:
        return None
    return None


def save_access_token(token: str):
    """Save access token to file"""
    try:
        with open(ACCESS_TOKEN_STORE_PATH, "w") as f:
            f.write(token)
    except Exception as e:
        logger.warning(f"Warning: Could not save access token: {e}")


def get_effective_superset_context(ctx: Context) -> SupersetContext:
    """
    Return the Superset context for this request.
    When HTTP Basic Auth is used, returns the per-request context (that user's Superset token).
    Otherwise returns the lifespan context (.env or stored token).
    """
    try:
        request_ctx = _current_request_superset_context.get()
        if request_ctx is not None:
            return request_ctx
    except LookupError:
        pass
    return ctx.request_context.lifespan_context


def ensure_token_loaded(ctx: Context) -> None:
    """
    If the current context has no access token, try loading from disk.
    HTTP/streamable-http mode may use a new session per request, so in-memory
    token from a previous authenticate_user call can be missing; loading from
    file keeps auth valid across tool calls. Skipped when using HTTP Basic Auth (per-request context).
    """
    try:
        superset_ctx = get_effective_superset_context(ctx)
    except (LookupError, AttributeError):
        return
    if superset_ctx.access_token:
        return
    try:
        request_ctx = _current_request_superset_context.get()
        if request_ctx is not None:
            return  # HTTP Basic Auth context has no disk token to load
    except LookupError:
        pass
    stored = load_stored_token()
    if not stored:
        return
    superset_ctx.access_token = stored
    superset_ctx.client.headers.update({"Authorization": f"Bearer {stored}"})
    logger.debug("Loaded access token from file for this request")


def _normalize_access_token_response(data: Dict[str, Any]) -> Optional[str]:
    """
    Extract access_token from login/refresh response.
    Superset 4.x–6.x may return { "access_token": "..." } or { "result": { "access_token": "..." } }.
    """
    if not data:
        return None
    token = data.get("access_token")
    if token:
        return token
    result = data.get("result")
    if isinstance(result, dict):
        return result.get("access_token")
    return None


def _normalize_csrf_response(data: Dict[str, Any]) -> Optional[str]:
    """
    Extract CSRF token from response.
    Handles { "result": "token" }, nested dict result, or top-level csrf_token.
    """
    if not data:
        return None
    token = data.get("result")
    if token is not None:
        if isinstance(token, dict):
            token = token.get("csrf_token") or token.get("token")
        if token is not None:
            s = str(token).strip()
            return s if s else None
    top = data.get("csrf_token")
    if top is not None:
        s = str(top).strip()
        return s if s else None
    return None


def _normalize_api_response(data: Any) -> Any:
    """
    Normalize API response for Superset 4.x–6.x compatibility.
    Many list/detail endpoints wrap payload in "result"; we return the full response
    but tools can rely on consistent structure. For generic make_api_request we pass through.
    """
    if data is None:
        return data
    if isinstance(data, dict) and "result" in data and "count" not in data and "ids" not in data:
        # Single-result style: { "result": { ... } } -> keep full for backward compatibility
        pass
    return data


@asynccontextmanager
async def superset_lifespan(server: FastMCP) -> AsyncIterator[SupersetContext]:
    """Manage application lifecycle for Superset integration"""
    logger.info("Initializing Superset context...")

    logger.info(f"Superset base URL: {SUPERSET_BASE_URL}")
    # Create HTTP client
    client = httpx.AsyncClient(base_url=SUPERSET_BASE_URL, timeout=30.0)

    # Create context
    ctx = SupersetContext(client=client, base_url=SUPERSET_BASE_URL, app=app)

    # Try to load existing token
    stored_token = load_stored_token()
    if stored_token:
        ctx.access_token = stored_token
        # Set the token in the client headers
        client.headers.update({"Authorization": f"Bearer {stored_token}"})
        logger.info("Using stored access token")

        # Verify token validity (Superset 6.0 uses same /api/v1/me/)
        try:
            response = await client.get(ME_ENDPOINT)
            if response.status_code != 200:
                logger.info(
                    f"Stored token is invalid (status {response.status_code}). Will need to re-authenticate."
                )
                ctx.access_token = None
                client.headers.pop("Authorization", None)
        except Exception as e:
            logger.info(f"Error verifying stored token: {e}")
            ctx.access_token = None
            client.headers.pop("Authorization", None)

    try:
        yield ctx
    finally:
        # Cleanup on shutdown
        logger.info("Shutting down Superset context...")
        await client.aclose()


# HTTP bind address: set FASTMCP_HOST=0.0.0.0 on server so Kimi/other clients can connect remotely
MCP_HOST = os.getenv("FASTMCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("FASTMCP_PORT", "8000"))

# Initialize FastMCP server with lifespan and dependencies
mcp = FastMCP(
    "superset",
    lifespan=superset_lifespan,
    dependencies=["fastapi", "uvicorn", "python-dotenv", "httpx"],
    host=MCP_HOST,
    port=MCP_PORT,
)

# Type variables for generic function annotations
T = TypeVar("T")
R = TypeVar("R")

# ===== Helper Functions and Decorators =====


def requires_auth(
    func: Callable[..., Awaitable[Dict[str, Any]]],
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """Decorator to check authentication before executing a function"""

    @wraps(func)
    async def wrapper(ctx: Context, *args, **kwargs) -> Dict[str, Any]:
        ensure_token_loaded(ctx)
        superset_ctx = get_effective_superset_context(ctx)

        if not superset_ctx.access_token:
            return {"error": "Not authenticated. Please authenticate first."}

        return await func(ctx, *args, **kwargs)

    return wrapper


def handle_api_errors(
    func: Callable[..., Awaitable[Dict[str, Any]]],
) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """Decorator to handle API errors in a consistent way"""

    @wraps(func)
    async def wrapper(ctx: Context, *args, **kwargs) -> Dict[str, Any]:
        try:
            return await func(ctx, *args, **kwargs)
        except Exception as e:
            # Extract function name for better error context
            function_name = func.__name__
            return {"error": f"Error in {function_name}: {str(e)}"}

    return wrapper


async def with_auto_refresh(
    ctx: Context, api_call: Callable[[], Awaitable[httpx.Response]]
) -> httpx.Response:
    """
    Helper function to handle automatic token refreshing for API calls

    This function will attempt to execute the provided API call. If the call
    fails with a 401 Unauthorized error, it will try to refresh the token
    and retry the API call once.

    Args:
        ctx: The MCP context
        api_call: The API call function to execute (should be a callable that returns a response)
    """
    superset_ctx = get_effective_superset_context(ctx)

    if not superset_ctx.access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # First attempt
    try:
        response = await api_call()

        # If not an auth error, return the response
        if response.status_code != 401:
            return response

    except httpx.HTTPStatusError as e:
        if e.response.status_code != 401:
            raise e
        response = e.response
    except Exception as e:
        # For other errors, just raise
        raise e

    # If we got a 401, try to refresh the token
    logger.info("Received 401 Unauthorized. Attempting to refresh token...")
    refresh_result = await superset_auth_refresh_token(ctx)

    if refresh_result.get("error"):
        # If refresh failed, try to re-authenticate
        logger.info(
            f"Token refresh failed: {refresh_result.get('error')}. Attempting re-authentication..."
        )
        auth_result = await superset_auth_authenticate_user(ctx)

        if auth_result.get("error"):
            # If re-authentication failed, raise an exception
            raise HTTPException(status_code=401, detail="Authentication failed")

    # New JWT may need a fresh CSRF bound to the same cookie session
    await _fetch_csrf_for_context(superset_ctx)

    # Retry the API call with the new token
    return await api_call()


def _csrf_paths_to_try() -> List[str]:
    """Trailing slash varies by proxy/FAB config; try both."""
    root = SECURITY_CSRF_ENDPOINT.rstrip("/")
    ordered = [root + "/", root]
    seen = set()
    out: List[str] = []
    for p in ordered:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _csrf_failure_suggests_reauth(detail: str) -> bool:
    return "HTTP 401" in detail or " 401 " in detail


def _csrf_failure_suggests_forbidden(detail: str) -> bool:
    return "HTTP 403" in detail or " 403 " in detail


async def _fetch_csrf_for_context(superset_ctx: SupersetContext) -> Tuple[Optional[str], str]:
    """
    Fetch a CSRF token from Superset and attach it to the shared httpx client.

    Returns (token, "") on success, or (None, diagnostic) on failure. The diagnostic
    includes HTTP status and response body snippets so operators can see 401 vs 403
    vs HTML error pages.

    Superset ties CSRF validation to the session cookie jar; the same AsyncClient
    must perform this GET and the subsequent POST. The token is also set on
    client.headers["X-CSRFToken"] so every mutating request includes it.
    """
    client = superset_ctx.client

    if superset_ctx.access_token:
        client.headers["Authorization"] = f"Bearer {superset_ctx.access_token}"

    extra_headers = {"Accept": "application/json"}

    last_err = (
        "No CSRF URL returned HTTP 200 with a parseable token. "
        f"Tried: {_csrf_paths_to_try()}"
    )
    for path in _csrf_paths_to_try():
        try:
            response = await client.get(path, headers=extra_headers)
        except Exception as e:
            last_err = f"GET {path!r} failed: {e}"
            logger.warning("CSRF fetch: %s", last_err)
            continue

        if response.status_code != 200:
            last_err = (
                f"GET {path!r} -> HTTP {response.status_code}. "
                f"Body (truncated): {response.text[:1500]}"
            )
            logger.warning("CSRF fetch: %s", last_err)
            continue

        try:
            data = response.json()
        except Exception as e:
            last_err = (
                f"GET {path!r} returned HTTP 200 but body is not JSON "
                f"(content-type={response.headers.get('content-type')!r}): "
                f"{response.text[:800]!r}. Parse error: {e}"
            )
            logger.warning("CSRF fetch: %s", last_err)
            continue

        csrf_token = _normalize_csrf_response(data)
        if csrf_token:
            superset_ctx.csrf_token = csrf_token
            client.headers["X-CSRFToken"] = csrf_token
            return csrf_token, ""

        last_err = (
            f"GET {path!r} returned JSON but no CSRF string could be parsed: {data!r}"
        )
        logger.warning("CSRF fetch: %s", last_err)

    superset_ctx.csrf_token = None
    client.headers.pop("X-CSRFToken", None)
    return None, last_err


async def get_csrf_token(ctx: Context) -> Optional[str]:
    """Fetch CSRF token for the current MCP request's Superset context."""
    token, _ = await _fetch_csrf_for_context(get_effective_superset_context(ctx))
    return token


async def _ensure_csrf_for_mutation(ctx: Context) -> Optional[str]:
    """
    Ensure a CSRF token is available for POST/PUT/DELETE.

    Returns None on success, or an error string for the tool response on failure.
    On HTTP 401 from the CSRF endpoint, tries JWT refresh then optional re-login
    using env credentials (same as superset_auth_authenticate_user).
    """
    superset_ctx = get_effective_superset_context(ctx)
    token, detail = await _fetch_csrf_for_context(superset_ctx)
    if token:
        return None

    if _csrf_failure_suggests_reauth(detail):
        refresh_result = await superset_auth_refresh_token(ctx)
        if not refresh_result.get("error"):
            token, detail = await _fetch_csrf_for_context(superset_ctx)
            if token:
                return None
        # Refresh alone often does not issue a new Flask session; CSRF may still 401 until
        # POST /login runs (force_login bypasses "Already authenticated" when /me still works).
        if SUPERSET_USERNAME and SUPERSET_PASSWORD:
            auth_result = await superset_auth_authenticate_user(
                ctx,
                username=SUPERSET_USERNAME,
                password=SUPERSET_PASSWORD,
                force_login=True,
            )
            if not auth_result.get("error"):
                token, detail = await _fetch_csrf_for_context(superset_ctx)
                if token:
                    return None

    extra = ""
    if _csrf_failure_suggests_forbidden(detail):
        extra = (
            " Hint: the JWT user may lack permission to call GET /api/v1/security/csrf_token/ "
            "(Flask-AppBuilder protect() on that route). Ask an admin to grant the role "
            "access to the Security API / CSRF endpoint."
        )
    elif _csrf_failure_suggests_reauth(detail):
        extra = (
            " Hint: set SUPERSET_USERNAME and SUPERSET_PASSWORD on the MCP server so a forced "
            f"re-login can run, or call superset_auth_authenticate_user(..., force_login=True). "
            f"You can also delete {ACCESS_TOKEN_STORE_PATH} and authenticate again."
        )

    return f"Could not obtain CSRF token from Superset. Detail: {detail}{extra}"


async def make_api_request(
    ctx: Context,
    method: str,
    endpoint: str,
    data: Dict[str, Any] = None,
    params: Dict[str, Any] = None,
    auto_refresh: bool = True,
) -> Dict[str, Any]:
    """
    Helper function to make API requests to Superset

    Args:
        ctx: MCP context
        method: HTTP method (get, post, put, delete)
        endpoint: API endpoint (without base URL)
        data: Optional JSON payload for POST/PUT requests
        params: Optional query parameters
        auto_refresh: Whether to auto-refresh token on 401
    """
    ensure_token_loaded(ctx)
    superset_ctx = get_effective_superset_context(ctx)
    client = superset_ctx.client

    # Mutating requests need a fresh CSRF token in the same cookie session as the POST.
    if method.lower() != "get":
        csrf_err = await _ensure_csrf_for_mutation(ctx)
        if csrf_err:
            return {"error": csrf_err}

    async def make_request() -> httpx.Response:
        headers = {}

        # Redundant with client.headers but keeps behavior explicit for proxies
        if method.lower() != "get" and superset_ctx.csrf_token:
            headers["X-CSRFToken"] = superset_ctx.csrf_token

        if method.lower() == "get":
            return await client.get(endpoint, params=params)
        elif method.lower() == "post":
            return await client.post(
                endpoint, json=data, params=params, headers=headers
            )
        elif method.lower() == "put":
            return await client.put(endpoint, json=data, headers=headers)
        elif method.lower() == "delete":
            return await client.delete(endpoint, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    # Use auto_refresh if requested
    response = (
        await with_auto_refresh(ctx, make_request)
        if auto_refresh
        else await make_request()
    )

    # One retry: stale session vs. CSRF (e.g. after token refresh without new CSRF)
    if (
        response.status_code == 400
        and method.lower() != "get"
        and "csrf" in (response.text or "").lower()
    ):
        await get_csrf_token(ctx)
        if superset_ctx.csrf_token:
            response = (
                await with_auto_refresh(ctx, make_request)
                if auto_refresh
                else await make_request()
            )

    if response.status_code not in [200, 201]:
        return {
            "error": f"API request failed: {response.status_code} - {response.text}"
        }

    try:
        data = response.json()
    except Exception as e:
        return {"error": f"Invalid JSON response: {e}"}

    return _normalize_api_response(data)


# ===== Authentication Tools =====


@mcp.tool()
@handle_api_errors
async def superset_auth_check_token_validity(ctx: Context) -> Dict[str, Any]:
    """
    Check if the current access token is still valid

    Makes a request to the /api/v1/me/ endpoint to test if the current token is valid.
    Use this to verify authentication status before making other API calls.

    Returns:
        A dictionary with token validity status and any error information
    """
    superset_ctx = get_effective_superset_context(ctx)

    if not superset_ctx.access_token:
        return {"valid": False, "error": "No access token available"}

    try:
        # Make a simple API call to test if token is valid (get user info)
        response = await superset_ctx.client.get(ME_ENDPOINT)

        if response.status_code == 200:
            return {"valid": True}
        else:
            return {
                "valid": False,
                "status_code": response.status_code,
                "error": response.text,
            }
    except Exception as e:
        return {"valid": False, "error": str(e)}


@mcp.tool()
@handle_api_errors
async def superset_auth_refresh_token(ctx: Context) -> Dict[str, Any]:
    """
    Refresh the access token using the refresh endpoint

    Makes a request to the /api/v1/security/refresh endpoint to get a new access token
    without requiring re-authentication with username/password.

    Returns:
        A dictionary with the new access token or error information
    """
    superset_ctx = get_effective_superset_context(ctx)

    if not superset_ctx.access_token:
        return {"error": "No access token to refresh. Please authenticate first."}

    try:
        # Superset 6.0: POST /api/v1/security/refresh (same as 4.x)
        response = await superset_ctx.client.post(SECURITY_REFRESH_ENDPOINT)

        if response.status_code != 200:
            return {
                "error": f"Failed to refresh token: {response.status_code} - {response.text}"
            }

        data = response.json()
        access_token = _normalize_access_token_response(data)

        if not access_token:
            return {"error": "No access token returned from refresh"}

        # Save and set the new access token
        save_access_token(access_token)
        superset_ctx.access_token = access_token
        superset_ctx.client.headers.update({"Authorization": f"Bearer {access_token}"})

        return {
            "message": "Successfully refreshed access token",
            "access_token": access_token,
        }
    except Exception as e:
        return {"error": f"Error refreshing token: {str(e)}"}


@mcp.tool()
@handle_api_errors
async def superset_auth_authenticate_user(
    ctx: Context,
    username: Optional[str] = None,
    password: Optional[str] = None,
    refresh: bool = True,
    force_login: bool = False,
) -> Dict[str, Any]:
    """
    Authenticate with Superset and get access token

    Makes a request to the /api/v1/security/login endpoint to authenticate and obtain an access token.
    If there's an existing token, will first try to check its validity.
    If invalid, will attempt to refresh token before falling back to re-authentication.

    Args:
        username: Superset username (falls back to environment variable if not provided)
        password: Superset password (falls back to environment variable if not provided)
        refresh: Whether to refresh the token if invalid (defaults to True)
        force_login: If True, skip the "already logged in" shortcut and POST /login again.
            Use this when the JWT is accepted by /me but CSRF still returns 401 — a full login
            establishes the Flask session cookie that pairs with generate_csrf().

    Returns:
        A dictionary with authentication status and access token or error information
    """
    superset_ctx = get_effective_superset_context(ctx)

    # If we already have a token, check if it's valid
    if not force_login and superset_ctx.access_token:
        validity = await superset_auth_check_token_validity(ctx)

        if validity.get("valid"):
            return {
                "message": "Already authenticated with valid token",
                "access_token": superset_ctx.access_token,
            }

        # Token invalid, try to refresh if requested
        if refresh:
            refresh_result = await superset_auth_refresh_token(ctx)
            if not refresh_result.get("error"):
                return refresh_result
            # If refresh fails, fall back to re-authentication

    # Use provided credentials or fall back to env vars
    username = username or SUPERSET_USERNAME
    password = password or SUPERSET_PASSWORD

    if not username or not password:
        return {
            "error": "Username and password must be provided either as arguments or set in environment variables"
        }

    try:
        # Superset 6.0: POST /api/v1/security/login with provider="db" (database auth)
        response = await superset_ctx.client.post(
            SECURITY_LOGIN_ENDPOINT,
            json={
                "username": username,
                "password": password,
                "provider": AUTH_PROVIDER,
                "refresh": refresh,
            },
        )

        if response.status_code != 200:
            return {
                "error": f"Failed to get access token: {response.status_code} - {response.text}"
            }

        data = response.json()
        access_token = _normalize_access_token_response(data)

        if not access_token:
            return {"error": "No access token returned"}

        # Save and set the access token
        save_access_token(access_token)
        superset_ctx.access_token = access_token
        superset_ctx.client.headers.update({"Authorization": f"Bearer {access_token}"})

        # Some Superset setups require session cookies for /api/v1/dashboard/ and /chart/ to return data (see apache/superset#25890)
        if getattr(response, "cookies", None):
            try:
                for name, value in response.cookies.items():
                    superset_ctx.client.cookies.set(name, value)
            except Exception as e:
                logger.debug("Could not set cookies from login response: %s", e)

        # Get CSRF token after successful authentication
        await get_csrf_token(ctx)

        return {
            "message": "Successfully authenticated with Superset",
            "access_token": access_token,
        }

    except Exception as e:
        return {"error": f"Authentication error: {str(e)}"}


# ===== Dashboard Tools =====


# When dashboard/chart list is empty despite dashboards existing in UI (Superset bug)
DASHBOARD_EMPTY_HINT = (
    "_diagnostic: If you have dashboards in the Superset UI but API returns count 0, "
    "this is a known Superset issue (apache/superset#25890). Fix: In Superset go to "
    "Settings → List Roles → Public → Edit, and REMOVE 'can read on Dashboard' and "
    "'can read on Chart' from the Public role. Do not add these permissions to Public."
)


def _normalize_dashboard_list_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize dashboard list API response for 4.x–6.x and clearer LLM consumption.
    API returns { count, ids?, result? }; ensure 'dashboards' and 'count' are explicit.
    """
    if not data or not isinstance(data, dict):
        return data
    out = dict(data)
    # Expose result array as 'dashboards' so assistants clearly see the list
    if "result" in data and isinstance(data["result"], list):
        out["dashboards"] = data["result"]
    elif "ids" in data and isinstance(data["ids"], list) and "dashboards" not in out:
        out["dashboards"] = [{"id": i} for i in data["ids"]]
    if "count" not in out and "dashboards" in out:
        out["count"] = len(out["dashboards"])
    if out.get("count") == 0 and (not out.get("dashboards") or len(out.get("dashboards", [])) == 0):
        out["_hint"] = DASHBOARD_EMPTY_HINT
    return out


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dashboard_list(
    ctx: Context, page_size: int = 100
) -> Dict[str, Any]:
    """
    Get a list of dashboards from Superset

    Makes a request to the /api/v1/dashboard/ endpoint to retrieve dashboards
    the current user has access to view. Uses pagination (page_size) so results
    are not limited to the default 20.

    Args:
        page_size: Number of dashboards to return per page (default 100). Use a larger value to see more.

    Returns:
        A dictionary with 'count', 'dashboards' (array of dashboard objects with id, dashboard_title, url, etc.), and raw API fields.
        If count is 0 but the user has dashboards in the UI, the response may include _hint about Superset Public role permissions (see apache/superset#25890).
    """
    # Superset list API requires pagination via 'q'. Default page_size=20 can truncate or confuse clients.
    page_size = max(1, min(page_size, 1000))
    # Try JSON first (Superset 6.x), then Rison (page:0,page_size:N) for older versions
    for q_val in (
        json.dumps({"page": 0, "page_size": page_size}),
        f"(page:0,page_size:{page_size})",
    ):
        result = await make_api_request(
            ctx, "get", "/api/v1/dashboard/", params={"q": q_val}
        )
        if isinstance(result, dict) and "error" not in result:
            return _normalize_dashboard_list_response(result)
        # If error (e.g. 400 for unsupported q format), try next format
    return result if isinstance(result, dict) else {"error": "Failed to list dashboards"}


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dashboard_get_by_id(
    ctx: Context, dashboard_id: int
) -> Dict[str, Any]:
    """
    Get details for a specific dashboard

    Makes a request to the /api/v1/dashboard/{id} endpoint to retrieve detailed
    information about a specific dashboard.

    Args:
        dashboard_id: ID of the dashboard to retrieve

    Returns:
        A dictionary with complete dashboard information including components and layout
    """
    return await make_api_request(ctx, "get", f"/api/v1/dashboard/{dashboard_id}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dashboard_create(
    ctx: Context, dashboard_title: str, json_metadata: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Create a new dashboard in Superset

    Makes a request to the /api/v1/dashboard/ POST endpoint to create a new dashboard.

    Args:
        dashboard_title: Title of the dashboard
        json_metadata: Optional JSON metadata for dashboard configuration,
                       can include layout, color scheme, and filter configuration

    Returns:
        A dictionary with the created dashboard information including its ID
    """
    payload = {"dashboard_title": dashboard_title}
    if json_metadata:
        payload["json_metadata"] = json_metadata

    return await make_api_request(ctx, "post", "/api/v1/dashboard/", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dashboard_update(
    ctx: Context, dashboard_id: int, data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Update an existing dashboard

    Makes a request to the /api/v1/dashboard/{id} PUT endpoint to update
    dashboard properties.

    Args:
        dashboard_id: ID of the dashboard to update
        data: Data to update, can include dashboard_title, slug, owners, position, and metadata

    Returns:
        A dictionary with the updated dashboard information
    """
    return await make_api_request(
        ctx, "put", f"/api/v1/dashboard/{dashboard_id}", data=data
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dashboard_delete(ctx: Context, dashboard_id: int) -> Dict[str, Any]:
    """
    Delete a dashboard

    Makes a request to the /api/v1/dashboard/{id} DELETE endpoint to remove a dashboard.
    This operation is permanent and cannot be undone.

    Args:
        dashboard_id: ID of the dashboard to delete

    Returns:
        A dictionary with deletion confirmation message
    """
    response = await make_api_request(
        ctx, "delete", f"/api/v1/dashboard/{dashboard_id}"
    )

    # For delete endpoints, we may want a custom success message
    if not response.get("error"):
        return {"message": f"Dashboard {dashboard_id} deleted successfully"}

    return response


# ===== Chart Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_chart_list(ctx: Context) -> Dict[str, Any]:
    """
    Get a list of charts from Superset

    Makes a request to the /api/v1/chart/ endpoint to retrieve all charts
    the current user has access to view. Results are paginated.

    Returns:
        A dictionary containing chart data including id, slice_name, viz_type, and datasource info
    """
    return await make_api_request(ctx, "get", "/api/v1/chart/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_chart_get_by_id(ctx: Context, chart_id: int) -> Dict[str, Any]:
    """
    Get details for a specific chart

    Makes a request to the /api/v1/chart/{id} endpoint to retrieve detailed
    information about a specific chart/slice.

    Args:
        chart_id: ID of the chart to retrieve

    Returns:
        A dictionary with complete chart information including visualization configuration
    """
    return await make_api_request(ctx, "get", f"/api/v1/chart/{chart_id}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_chart_create(
    ctx: Context,
    slice_name: str,
    datasource_id: int,
    datasource_type: str,
    viz_type: str,
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Create a new chart in Superset

    Makes a request to the /api/v1/chart/ POST endpoint to create a new visualization.

    Args:
        slice_name: Name/title of the chart
        datasource_id: ID of the dataset or SQL table
        datasource_type: Type of datasource ('table' for datasets, 'query' for SQL)
        viz_type: Visualization type (e.g., 'bar', 'line', 'pie', 'big_number', etc.)
        params: Visualization parameters including metrics, groupby, time_range, etc.

    Returns:
        A dictionary with the created chart information including its ID
    """
    payload = {
        "slice_name": slice_name,
        "datasource_id": datasource_id,
        "datasource_type": datasource_type,
        "viz_type": viz_type,
        "params": json.dumps(params),
    }

    return await make_api_request(ctx, "post", "/api/v1/chart/", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_chart_update(
    ctx: Context, chart_id: int, data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Update an existing chart

    Makes a request to the /api/v1/chart/{id} PUT endpoint to update
    chart properties and visualization settings.

    Args:
        chart_id: ID of the chart to update
        data: Data to update, can include slice_name, description, viz_type, params, etc.

    Returns:
        A dictionary with the updated chart information
    """
    return await make_api_request(ctx, "put", f"/api/v1/chart/{chart_id}", data=data)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_chart_delete(ctx: Context, chart_id: int) -> Dict[str, Any]:
    """
    Delete a chart

    Makes a request to the /api/v1/chart/{id} DELETE endpoint to remove a chart.
    This operation is permanent and cannot be undone.

    Args:
        chart_id: ID of the chart to delete

    Returns:
        A dictionary with deletion confirmation message
    """
    response = await make_api_request(ctx, "delete", f"/api/v1/chart/{chart_id}")

    if not response.get("error"):
        return {"message": f"Chart {chart_id} deleted successfully"}

    return response


# ===== Database Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_list(ctx: Context) -> Dict[str, Any]:
    """
    Get a list of databases from Superset

    Makes a request to the /api/v1/database/ endpoint to retrieve all database
    connections the current user has access to. Results are paginated.

    Returns:
        A dictionary containing database connection information including id, name, and configuration
    """
    return await make_api_request(ctx, "get", "/api/v1/database/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_get_by_id(ctx: Context, database_id: int) -> Dict[str, Any]:
    """
    Get details for a specific database

    Makes a request to the /api/v1/database/{id} endpoint to retrieve detailed
    information about a specific database connection.

    Args:
        database_id: ID of the database to retrieve

    Returns:
        A dictionary with complete database configuration information
    """
    return await make_api_request(ctx, "get", f"/api/v1/database/{database_id}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_create(
    ctx: Context,
    engine: str,
    configuration_method: str,
    database_name: str,
    sqlalchemy_uri: str,
) -> Dict[str, Any]:
    """
    Create a new database connection in Superset

    IMPORTANT: Don't call this tool, unless user have given connection details. This function will only create database connections with explicit user consent and input.
    No default values or assumptions will be made without user confirmation. All connection parameters,
    including sensitive credentials, must be explicitly provided by the user.

    Makes a POST request to /api/v1/database/ to create a new database connection in Superset.
    The endpoint requires a valid SQLAlchemy URI and database configuration parameters.
    The engine parameter will be automatically determined from the SQLAlchemy URI prefix if not specified:
    - 'postgresql://' -> engine='postgresql'
    - 'mysql://' -> engine='mysql'
    - 'mssql://' -> engine='mssql'
    - 'oracle://' -> engine='oracle'
    - 'sqlite://' -> engine='sqlite'

    The SQLAlchemy URI must follow the format: dialect+driver://username:password@host:port/database
    If the URI is not provided, the function will prompt for individual connection parameters to construct it.

    All required parameters must be provided and validated before creating the connection.
    The configuration_method parameter should typically be set to 'sqlalchemy_form'.

    Args:
        engine: Database engine (e.g., 'postgresql', 'mysql', etc.)
        configuration_method: Method used for configuration (typically 'sqlalchemy_form')
        database_name: Name for the database connection
        sqlalchemy_uri: SQLAlchemy URI for the connection (e.g., 'postgresql://user:pass@host/db')

    Returns:
        A dictionary with the created database connection information including its ID
    """
    payload = {
        "engine": engine,
        "configuration_method": configuration_method,
        "database_name": database_name,
        "sqlalchemy_uri": sqlalchemy_uri,
        "allow_dml": True,
        "allow_cvas": True,
        "allow_ctas": True,
        "expose_in_sqllab": True,
    }

    return await make_api_request(ctx, "post", "/api/v1/database/", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_get_tables(
    ctx: Context, database_id: int
) -> Dict[str, Any]:
    """
    Get a list of tables for a given database

    Makes a request to the /api/v1/database/{id}/tables/ endpoint to retrieve
    all tables available in the database.

    Args:
        database_id: ID of the database

    Returns:
        A dictionary with list of tables including schema and table name information
    """
    return await make_api_request(ctx, "get", f"/api/v1/database/{database_id}/tables/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_schemas(ctx: Context, database_id: int) -> Dict[str, Any]:
    """
    Get schemas for a specific database

    Makes a request to the /api/v1/database/{id}/schemas/ endpoint to retrieve
    all schemas available in the database.

    Args:
        database_id: ID of the database

    Returns:
        A dictionary with list of schema names
    """
    return await make_api_request(
        ctx, "get", f"/api/v1/database/{database_id}/schemas/"
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_test_connection(
    ctx: Context, database_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Test a database connection

    Makes a request to the /api/v1/database/test_connection endpoint to verify if
    the provided connection details can successfully connect to the database.

    Args:
        database_data: Database connection details including sqlalchemy_uri and other parameters

    Returns:
        A dictionary with connection test results
    """
    return await make_api_request(
        ctx, "post", "/api/v1/database/test_connection", data=database_data
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_update(
    ctx: Context, database_id: int, data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Update an existing database connection

    Makes a request to the /api/v1/database/{id} PUT endpoint to update
    database connection properties.

    Args:
        database_id: ID of the database to update
        data: Data to update, can include database_name, sqlalchemy_uri, password, and extra configs

    Returns:
        A dictionary with the updated database information
    """
    return await make_api_request(
        ctx, "put", f"/api/v1/database/{database_id}", data=data
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_delete(ctx: Context, database_id: int) -> Dict[str, Any]:
    """
    Delete a database connection

    Makes a request to the /api/v1/database/{id} DELETE endpoint to remove a database connection.
    This operation is permanent and cannot be undone. This will also remove associated datasets.

    Args:
        database_id: ID of the database to delete

    Returns:
        A dictionary with deletion confirmation message
    """
    response = await make_api_request(ctx, "delete", f"/api/v1/database/{database_id}")

    if not response.get("error"):
        return {"message": f"Database {database_id} deleted successfully"}

    return response


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_get_catalogs(
    ctx: Context, database_id: int
) -> Dict[str, Any]:
    """
    Get all catalogs from a database

    Makes a request to the /api/v1/database/{id}/catalogs/ endpoint to retrieve
    all catalogs available in the database.

    Args:
        database_id: ID of the database

    Returns:
        A dictionary with list of catalog names for databases that support catalogs
    """
    return await make_api_request(
        ctx, "get", f"/api/v1/database/{database_id}/catalogs/"
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_get_connection(
    ctx: Context, database_id: int
) -> Dict[str, Any]:
    """
    Get database connection information

    Makes a request to the /api/v1/database/{id}/connection endpoint to retrieve
    connection details for a specific database.

    Args:
        database_id: ID of the database

    Returns:
        A dictionary with detailed connection information
    """
    return await make_api_request(
        ctx, "get", f"/api/v1/database/{database_id}/connection"
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_get_function_names(
    ctx: Context, database_id: int
) -> Dict[str, Any]:
    """
    Get function names supported by a database

    Makes a request to the /api/v1/database/{id}/function_names/ endpoint to retrieve
    all SQL functions supported by the database.

    Args:
        database_id: ID of the database

    Returns:
        A dictionary with list of supported function names
    """
    return await make_api_request(
        ctx, "get", f"/api/v1/database/{database_id}/function_names/"
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_get_related_objects(
    ctx: Context, database_id: int
) -> Dict[str, Any]:
    """
    Get charts and dashboards associated with a database

    Makes a request to the /api/v1/database/{id}/related_objects/ endpoint to retrieve
    counts and references of charts and dashboards that depend on this database.

    Args:
        database_id: ID of the database

    Returns:
        A dictionary with counts and lists of related charts and dashboards
    """
    return await make_api_request(
        ctx, "get", f"/api/v1/database/{database_id}/related_objects/"
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_validate_sql(
    ctx: Context, database_id: int, sql: str
) -> Dict[str, Any]:
    """
    Validate arbitrary SQL against a database

    Makes a request to the /api/v1/database/{id}/validate_sql/ endpoint to check
    if the provided SQL is valid for the specified database.

    Args:
        database_id: ID of the database
        sql: SQL query to validate

    Returns:
        A dictionary with validation results
    """
    payload = {"sql": sql}
    return await make_api_request(
        ctx, "post", f"/api/v1/database/{database_id}/validate_sql/", data=payload
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_database_validate_parameters(
    ctx: Context, parameters: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Validate database connection parameters

    Makes a request to the /api/v1/database/validate_parameters/ endpoint to verify
    if the provided connection parameters are valid without creating a connection.

    Args:
        parameters: Connection parameters to validate

    Returns:
        A dictionary with validation results
    """
    return await make_api_request(
        ctx, "post", "/api/v1/database/validate_parameters/", data=parameters
    )


# ===== Dataset Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dataset_list(ctx: Context) -> Dict[str, Any]:
    """
    Get a list of datasets from Superset

    Makes a request to the /api/v1/dataset/ endpoint to retrieve all datasets
    the current user has access to view. Results are paginated.

    Returns:
        A dictionary containing dataset information including id, table_name, and database
    """
    return await make_api_request(ctx, "get", "/api/v1/dataset/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dataset_get_by_id(ctx: Context, dataset_id: int) -> Dict[str, Any]:
    """
    Get details for a specific dataset

    Makes a request to the /api/v1/dataset/{id} endpoint to retrieve detailed
    information about a specific dataset including columns and metrics.

    Args:
        dataset_id: ID of the dataset to retrieve

    Returns:
        A dictionary with complete dataset information
    """
    return await make_api_request(ctx, "get", f"/api/v1/dataset/{dataset_id}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_dataset_create(
    ctx: Context,
    table_name: str,
    database_id: int,
    schema: str = None,
    owners: List[int] = None,
) -> Dict[str, Any]:
    """
    Create a new dataset in Superset

    Makes a request to the /api/v1/dataset/ POST endpoint to create a new dataset
    from an existing database table or view.

    Args:
        table_name: Name of the physical table in the database
        database_id: ID of the database where the table exists
        schema: Optional database schema name where the table is located
        owners: Optional list of user IDs who should own this dataset

    Returns:
        A dictionary with the created dataset information including its ID
    """
    payload = {
        "table_name": table_name,
        "database": database_id,
    }

    if schema:
        payload["schema"] = schema

    if owners:
        payload["owners"] = owners

    return await make_api_request(ctx, "post", "/api/v1/dataset/", data=payload)


# ===== SQL Lab Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_execute_query(
    ctx: Context, database_id: int, sql: str
) -> Dict[str, Any]:
    """
    Execute a SQL query in SQL Lab

    Makes a request to the /api/v1/sqllab/execute/ endpoint to run a SQL query
    against the specified database.

    Args:
        database_id: ID of the database to query
        sql: SQL query to execute

    Returns:
        A dictionary with query results or execution status for async queries
    """
    payload = {
        "database_id": database_id,
        "sql": sql,
        "schema": "",
        "tab": "MCP Query",
        "runAsync": False,
        "select_as_cta": False,
    }

    return await make_api_request(ctx, "post", "/api/v1/sqllab/execute/", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_get_saved_queries(ctx: Context) -> Dict[str, Any]:
    """
    Get a list of saved queries from SQL Lab

    Makes a request to the /api/v1/saved_query/ endpoint to retrieve all saved queries
    the current user has access to. Results are paginated.

    Returns:
        A dictionary containing saved query information including id, label, and database
    """
    return await make_api_request(ctx, "get", "/api/v1/saved_query/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_format_sql(ctx: Context, sql: str) -> Dict[str, Any]:
    """
    Format a SQL query for better readability

    Makes a request to the /api/v1/sqllab/format_sql endpoint to apply standard
    formatting rules to the provided SQL query.

    Args:
        sql: SQL query to format

    Returns:
        A dictionary with the formatted SQL
    """
    payload = {"sql": sql}
    return await make_api_request(
        ctx, "post", "/api/v1/sqllab/format_sql", data=payload
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_get_results(ctx: Context, key: str) -> Dict[str, Any]:
    """
    Get results of a previously executed SQL query

    Makes a request to the /api/v1/sqllab/results/ endpoint to retrieve results
    for an asynchronous query using its result key.

    Args:
        key: Result key to retrieve

    Returns:
        A dictionary with query results including column information and data rows
    """
    return await make_api_request(
        ctx, "get", f"/api/v1/sqllab/results/", params={"key": key}
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_estimate_query_cost(
    ctx: Context, database_id: int, sql: str, schema: str = None
) -> Dict[str, Any]:
    """
    Estimate the cost of executing a SQL query

    Makes a request to the /api/v1/sqllab/estimate endpoint to get approximate cost
    information for a query before executing it.

    Args:
        database_id: ID of the database
        sql: SQL query to estimate
        schema: Optional schema name

    Returns:
        A dictionary with estimated query cost metrics
    """
    payload = {
        "database_id": database_id,
        "sql": sql,
    }

    if schema:
        payload["schema"] = schema

    return await make_api_request(ctx, "post", "/api/v1/sqllab/estimate", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_export_query_results(
    ctx: Context, client_id: str
) -> Dict[str, Any]:
    """
    Export the results of a SQL query to CSV

    Makes a request to the /api/v1/sqllab/export/{client_id} endpoint to download
    query results in CSV format.

    Args:
        client_id: Client ID of the query

    Returns:
        A dictionary with the exported data or error information
    """
    superset_ctx = get_effective_superset_context(ctx)

    try:
        response = await superset_ctx.client.get(f"/api/v1/sqllab/export/{client_id}")

        if response.status_code != 200:
            return {
                "error": f"Failed to export query results: {response.status_code} - {response.text}"
            }

        return {"message": "Query results exported successfully", "data": response.text}

    except Exception as e:
        return {"error": f"Error exporting query results: {str(e)}"}


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_sqllab_get_bootstrap_data(ctx: Context) -> Dict[str, Any]:
    """
    Get the bootstrap data for SQL Lab

    Makes a request to the /api/v1/sqllab/ endpoint to retrieve configuration data
    needed for the SQL Lab interface.

    Returns:
        A dictionary with SQL Lab configuration including allowed databases and settings
    """
    return await make_api_request(ctx, "get", "/api/v1/sqllab/")


# ===== Saved Query Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_saved_query_get_by_id(ctx: Context, query_id: int) -> Dict[str, Any]:
    """
    Get details for a specific saved query

    Makes a request to the /api/v1/saved_query/{id} endpoint to retrieve information
    about a saved SQL query.

    Args:
        query_id: ID of the saved query to retrieve

    Returns:
        A dictionary with the saved query details including SQL text and database
    """
    return await make_api_request(ctx, "get", f"/api/v1/saved_query/{query_id}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_saved_query_create(
    ctx: Context, query_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create a new saved query

    Makes a request to the /api/v1/saved_query/ POST endpoint to save a SQL query
    for later reuse.

    Args:
        query_data: Dictionary containing the query information including:
                   - db_id: Database ID
                   - schema: Schema name (optional)
                   - sql: SQL query text
                   - label: Display name for the saved query
                   - description: Optional description of the query

    Returns:
        A dictionary with the created saved query information including its ID
    """
    return await make_api_request(ctx, "post", "/api/v1/saved_query/", data=query_data)


# ===== Query Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_query_stop(ctx: Context, client_id: str) -> Dict[str, Any]:
    """
    Stop a running query

    Makes a request to the /api/v1/query/stop endpoint to terminate a query that
    is currently running.

    Args:
        client_id: Client ID of the query to stop

    Returns:
        A dictionary with confirmation of query termination
    """
    payload = {"client_id": client_id}
    return await make_api_request(ctx, "post", "/api/v1/query/stop", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_query_list(ctx: Context) -> Dict[str, Any]:
    """
    Get a list of queries from Superset

    Makes a request to the /api/v1/query/ endpoint to retrieve query history.
    Results are paginated and include both finished and running queries.

    Returns:
        A dictionary containing query information including status, duration, and SQL
    """
    return await make_api_request(ctx, "get", "/api/v1/query/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_query_get_by_id(ctx: Context, query_id: int) -> Dict[str, Any]:
    """
    Get details for a specific query

    Makes a request to the /api/v1/query/{id} endpoint to retrieve detailed
    information about a specific query execution.

    Args:
        query_id: ID of the query to retrieve

    Returns:
        A dictionary with complete query execution information
    """
    return await make_api_request(ctx, "get", f"/api/v1/query/{query_id}")


# ===== Activity and User Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_activity_get_recent(ctx: Context) -> Dict[str, Any]:
    """
    Get recent activity data for the current user

    Makes a request to the /api/v1/log/recent_activity/ endpoint to retrieve
    a history of actions performed by the current user.

    Returns:
        A dictionary with recent user activities including viewed charts and dashboards
    """
    return await make_api_request(ctx, "get", "/api/v1/log/recent_activity/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_user_get_current(ctx: Context) -> Dict[str, Any]:
    """
    Get information about the currently authenticated user

    Makes a request to the /api/v1/me/ endpoint to retrieve the user's profile
    information including permissions and preferences.

    Returns:
        A dictionary with user profile data
    """
    return await make_api_request(ctx, "get", "/api/v1/me/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_user_get_roles(ctx: Context) -> Dict[str, Any]:
    """
    Get roles for the current user

    Makes a request to the /api/v1/me/roles/ endpoint to retrieve all roles
    assigned to the current user.

    Returns:
        A dictionary with user role information
    """
    return await make_api_request(ctx, "get", "/api/v1/me/roles/")


# ===== Tag Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_list(ctx: Context) -> Dict[str, Any]:
    """
    Get a list of tags from Superset

    Makes a request to the /api/v1/tag/ endpoint to retrieve all tags
    defined in the Superset instance.

    Returns:
        A dictionary containing tag information including id and name
    """
    return await make_api_request(ctx, "get", "/api/v1/tag/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_create(ctx: Context, name: str) -> Dict[str, Any]:
    """
    Create a new tag in Superset

    Makes a request to the /api/v1/tag/ POST endpoint to create a new tag
    that can be applied to objects like charts and dashboards.

    Args:
        name: Name for the tag

    Returns:
        A dictionary with the created tag information
    """
    payload = {"name": name}
    return await make_api_request(ctx, "post", "/api/v1/tag/", data=payload)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_get_by_id(ctx: Context, tag_id: int) -> Dict[str, Any]:
    """
    Get details for a specific tag

    Makes a request to the /api/v1/tag/{id} endpoint to retrieve information
    about a specific tag.

    Args:
        tag_id: ID of the tag to retrieve

    Returns:
        A dictionary with tag details
    """
    return await make_api_request(ctx, "get", f"/api/v1/tag/{tag_id}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_objects(ctx: Context) -> Dict[str, Any]:
    """
    Get objects associated with tags

    Makes a request to the /api/v1/tag/get_objects/ endpoint to retrieve
    all objects that have tags assigned to them.

    Returns:
        A dictionary with tagged objects grouped by tag
    """
    return await make_api_request(ctx, "get", "/api/v1/tag/get_objects/")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_delete(ctx: Context, tag_id: int) -> Dict[str, Any]:
    """
    Delete a tag

    Makes a request to the /api/v1/tag/{id} DELETE endpoint to remove a tag.
    This operation is permanent and cannot be undone.

    Args:
        tag_id: ID of the tag to delete

    Returns:
        A dictionary with deletion confirmation message
    """
    response = await make_api_request(ctx, "delete", f"/api/v1/tag/{tag_id}")

    if not response.get("error"):
        return {"message": f"Tag {tag_id} deleted successfully"}

    return response


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_object_add(
    ctx: Context, object_type: str, object_id: int, tag_name: str
) -> Dict[str, Any]:
    """
    Add a tag to an object

    Makes a request to tag an object with a specific tag. This creates an association
    between the tag and the specified object (chart, dashboard, etc.)

    Args:
        object_type: Type of the object ('chart', 'dashboard', etc.)
        object_id: ID of the object to tag
        tag_name: Name of the tag to apply

    Returns:
        A dictionary with the tagging confirmation
    """
    payload = {
        "object_type": object_type,
        "object_id": object_id,
        "tag_name": tag_name,
    }

    return await make_api_request(
        ctx, "post", "/api/v1/tag/tagged_objects", data=payload
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_tag_object_remove(
    ctx: Context, object_type: str, object_id: int, tag_name: str
) -> Dict[str, Any]:
    """
    Remove a tag from an object

    Makes a request to remove a tag association from a specific object.

    Args:
        object_type: Type of the object ('chart', 'dashboard', etc.)
        object_id: ID of the object to untag
        tag_name: Name of the tag to remove

    Returns:
        A dictionary with the untagging confirmation message
    """
    response = await make_api_request(
        ctx,
        "delete",
        f"/api/v1/tag/{object_type}/{object_id}",
        params={"tag_name": tag_name},
    )

    if not response.get("error"):
        return {
            "message": f"Tag '{tag_name}' removed from {object_type} {object_id} successfully"
        }

    return response


# ===== Explore Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_explore_form_data_create(
    ctx: Context, form_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create form data for chart exploration

    Makes a request to the /api/v1/explore/form_data POST endpoint to store
    chart configuration data temporarily.

    Args:
        form_data: Chart configuration including datasource, metrics, and visualization settings

    Returns:
        A dictionary with a key that can be used to retrieve the form data
    """
    return await make_api_request(
        ctx, "post", "/api/v1/explore/form_data", data=form_data
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_explore_form_data_get(ctx: Context, key: str) -> Dict[str, Any]:
    """
    Get form data for chart exploration

    Makes a request to the /api/v1/explore/form_data/{key} endpoint to retrieve
    previously stored chart configuration.

    Args:
        key: Key of the form data to retrieve

    Returns:
        A dictionary with the stored chart configuration
    """
    return await make_api_request(ctx, "get", f"/api/v1/explore/form_data/{key}")


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_explore_permalink_create(
    ctx: Context, state: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create a permalink for chart exploration

    Makes a request to the /api/v1/explore/permalink POST endpoint to generate
    a shareable link to a specific chart exploration state.

    Args:
        state: State data for the permalink including form_data

    Returns:
        A dictionary with a key that can be used to access the permalink
    """
    return await make_api_request(ctx, "post", "/api/v1/explore/permalink", data=state)


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_explore_permalink_get(ctx: Context, key: str) -> Dict[str, Any]:
    """
    Get a permalink for chart exploration

    Makes a request to the /api/v1/explore/permalink/{key} endpoint to retrieve
    a previously saved exploration state.

    Args:
        key: Key of the permalink to retrieve

    Returns:
        A dictionary with the stored exploration state
    """
    return await make_api_request(ctx, "get", f"/api/v1/explore/permalink/{key}")


# ===== Menu Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_menu_get(ctx: Context) -> Dict[str, Any]:
    """
    Get the Superset menu data

    Makes a request to the /api/v1/menu/ endpoint to retrieve the navigation
    menu structure based on user permissions.

    Returns:
        A dictionary with menu items and their configurations
    """
    return await make_api_request(ctx, "get", "/api/v1/menu/")


# ===== Configuration Tools =====


@mcp.tool()
@handle_api_errors
async def superset_config_get_base_url(ctx: Context) -> Dict[str, Any]:
    """
    Get the base URL of the Superset instance

    Returns the configured Superset base URL that this MCP server is connecting to.
    This can be useful for constructing full URLs to Superset resources or for
    displaying information about the connected instance.

    This tool does not require authentication as it only returns configuration information.

    Returns:
        A dictionary with the Superset base URL
    """
    superset_ctx = get_effective_superset_context(ctx)

    return {
        "base_url": superset_ctx.base_url,
        "message": f"Connected to Superset instance at: {superset_ctx.base_url}",
    }


# ===== Advanced Data Type Tools =====


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_advanced_data_type_convert(
    ctx: Context, type_name: str, value: Any
) -> Dict[str, Any]:
    """
    Convert a value to an advanced data type

    Makes a request to the /api/v1/advanced_data_type/convert endpoint to transform
    a value into the specified advanced data type format.

    Args:
        type_name: Name of the advanced data type
        value: Value to convert

    Returns:
        A dictionary with the converted value
    """
    params = {
        "type_name": type_name,
        "value": value,
    }

    return await make_api_request(
        ctx, "get", "/api/v1/advanced_data_type/convert", params=params
    )


@mcp.tool()
@requires_auth
@handle_api_errors
async def superset_advanced_data_type_list(ctx: Context) -> Dict[str, Any]:
    """
    Get list of available advanced data types

    Makes a request to the /api/v1/advanced_data_type/types endpoint to retrieve
    all advanced data types supported by this Superset instance.

    Returns:
        A dictionary with available advanced data types and their configurations
    """
    return await make_api_request(ctx, "get", "/api/v1/advanced_data_type/types")


# ===== HTTP Basic Auth: per-request Superset credentials =====


async def _login_superset_and_create_context(username: str, password: str) -> Optional[SupersetContext]:
    """
    Log in to Superset with the given credentials and return a SupersetContext.
    Used by HTTP Basic Auth middleware; token is not persisted to disk.
    """
    client = httpx.AsyncClient(base_url=SUPERSET_BASE_URL, timeout=30.0)
    try:
        response = await client.post(
            SECURITY_LOGIN_ENDPOINT,
            json={
                "username": username,
                "password": password,
                "provider": AUTH_PROVIDER,
                "refresh": True,
            },
        )
        if response.status_code != 200:
            logger.warning("HTTP Basic Auth: Superset login failed for user %s: %s", username, response.status_code)
            return None
        data = response.json()
        access_token = _normalize_access_token_response(data)
        if not access_token:
            return None
        client.headers.update({"Authorization": f"Bearer {access_token}"})
        if getattr(response, "cookies", None):
            try:
                for name, value in response.cookies.items():
                    client.cookies.set(name, value)
            except Exception as e:
                logger.debug("HTTP Basic: could not set cookies from login: %s", e)
        ctx = SupersetContext(client=client, base_url=SUPERSET_BASE_URL, app=app)
        ctx.access_token = access_token
        _token, csrf_err = await _fetch_csrf_for_context(ctx)
        if not _token:
            logger.warning("HTTP Basic Auth: CSRF fetch failed for user %s: %s", username, csrf_err)
        return ctx
    except Exception as e:
        logger.warning("HTTP Basic Auth: Superset login error for user %s: %s", username, e)
        await client.aclose()
        return None


async def _send_401_require_basic_auth(send: Any) -> None:
    """Send 401 when Basic Auth was sent but invalid; omit Basic to use .env defaults."""
    body = (
        b'{"error":"Invalid Superset credentials in Authorization Basic. '
        b'Omit Basic Auth to use SUPERSET_USERNAME/SUPERSET_PASSWORD from server .env."}'
    )
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            [b"www-authenticate", b'Basic realm="Superset MCP"'],
            [b"content-type", b"application/json"],
            [b"content-length", str(len(body)).encode()],
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _superset_basic_auth_middleware(scope: dict, receive: Any, send: Any, next_app: Any) -> None:
    """
    ASGI middleware: optional Authorization: Basic (Superset username:password).
    If present and valid, use per-request Superset context. If omitted, use lifespan / .env default credentials.
    If Basic is present but invalid, return 401.
    Do not close the request client in finally: MCP may run the tool after next_app returns.
    Close the previous request's client when a new request starts.
    """
    # Close previous request's client (if any) when this request starts, so we don't close the current one in finally
    try:
        prev = _current_request_superset_context.get()
        if isinstance(prev, SupersetContext) and prev.client:
            await prev.client.aclose()
    except LookupError:
        pass
    _current_request_superset_context.set(None)
    try:
        if scope.get("type") == "http":
            method = scope.get("method", "?").upper()
            path = scope.get("path", "?")
            auth_header = None
            for name, value in scope.get("headers", []):
                if name.lower() == b"authorization" and value.startswith(b"Basic "):
                    auth_header = value
                    break
            logger.info("MCP request: %s %s has_basic_auth=%s", method, path, auth_header is not None)
            if auth_header:
                try:
                    # Decode Base64; strip padding/whitespace so trailing padding does not break decoding
                    b64 = auth_header[6:].strip()
                    pad = 4 - (len(b64) % 4)
                    if 0 < pad < 4:
                        b64 = b64 + b"=" * pad
                    raw = base64.b64decode(b64, validate=True).decode("utf-8")
                    # Per RFC 7617: first colon separates username from password. Password may contain colons.
                    if ":" not in raw:
                        await _send_401_require_basic_auth(send)
                        return
                    sup_user, sup_pass = raw.split(":", 1)
                    sup_user = sup_user.strip()
                    sup_pass = sup_pass.strip()
                    if not sup_user or not sup_pass:
                        await _send_401_require_basic_auth(send)
                        return
                    request_ctx = await _login_superset_and_create_context(sup_user, sup_pass)
                    if request_ctx is None:
                        logger.warning("MCP request rejected: Superset login failed for user %s", sup_user)
                        await _send_401_require_basic_auth(send)
                        return
                    _current_request_superset_context.set(request_ctx)
                    logger.info("HTTP Basic Auth: using Superset user %s for this request", sup_user)
                except Exception as e:
                    logger.warning("HTTP Basic Auth error: %s", e)
                    await _send_401_require_basic_auth(send)
                    return
            else:
                logger.info("MCP request: no Basic Auth, using default .env / lifespan Superset context")
            # MCP Streamable HTTP only accepts POST; GET returns 404 from the SDK. Return 405 with a clear message.
            path_normalized = (scope.get("path") or "").rstrip("/") or "/"
            if path_normalized == "/mcp" and method == "GET":
                body = b'{"message":"MCP endpoint accepts POST only. Use POST with JSON-RPC for tool calls."}'
                await send({
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [[b"content-type", b"application/json"], [b"content-length", str(len(body)).encode()]],
                })
                await send({"type": "http.response.body", "body": body})
                return
        logger.info("MCP request: auth OK, forwarding to MCP app")
        try:
            await next_app(scope, receive, send)
        except Exception as e:
            logger.exception("MCP app error: %s", e)
            raise
    finally:
        # Do NOT close the client or reset the context here: MCP may invoke the tool after next_app returns.
        # The next request will close this request's client at the start of the middleware.
        pass


def _wrap_app_with_superset_basic_auth(asgi_app: Any) -> Any:
    """Wrap the MCP ASGI app so that HTTP requests can carry Superset credentials via Basic Auth."""

    async def wrapped(scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await asgi_app(scope, receive, send)
            return
        await _superset_basic_auth_middleware(scope, receive, send, asgi_app)

    return wrapped


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Superset MCP server (compatible with Superset 4.x–6.x)"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport: stdio (default, for Claude Desktop), http (Streamable HTTP), sse (legacy SSE)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for http/sse transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for http/sse transport (default: 8000)",
    )
    args = parser.parse_args()

    logger.info("Starting Superset MCP server...")
    if args.transport == "stdio":
        mcp.run()
    elif args.transport == "http":
        # Wrap with Basic Auth middleware so clients can send Superset username:password via HTTP Basic
        starlette_app = mcp.streamable_http_app()
        wrapped_app = _wrap_app_with_superset_basic_auth(starlette_app)
        logger.info(
            "Listening on streamable-http at http://%s:%s (HTTP Basic Auth = Superset username:password)",
            MCP_HOST,
            MCP_PORT,
        )
        uvicorn.run(wrapped_app, host=MCP_HOST, port=MCP_PORT, log_level="info")
    else:
        sdk_transport = "sse"
        logger.info(f"Listening on {sdk_transport} at http://{args.host}:{args.port}")
        mcp.run(transport=sdk_transport)

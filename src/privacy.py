"""
Privacy filtering for Focus Guardian.

Five layers of sensitive data protection:
  1. App blocklist — skip entire apps (password managers, banking)
  2. Window state — skip hidden apps and private browser windows
  3. AX field filtering — skip secure text fields (passwords) in the UI tree
  4. URL scrubbing — strip sensitive query parameters before storage
  5. Sensitive page detection — block captures from password managers,
     banking pages, and other sensitive contexts inside browsers
"""

import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from config import CONFIG

_PRIVACY_CFG = CONFIG.get("privacy", {})

# Layer 1: blocked app names (lowercased for case-insensitive matching)
BLOCKED_APPS = {
    name.lower() for name in _PRIVACY_CFG.get("blocked_apps", [])
}

# Layer 3: blocked AX subroles
BLOCKED_AX_SUBROLES = set(_PRIVACY_CFG.get("blocked_ax_subroles", ["AXSecureTextField"]))

# Layer 4: sensitive URL parameter keys (lowercased)
SENSITIVE_URL_KEYS = {
    key.lower() for key in _PRIVACY_CFG.get("sensitive_url_keys", [])
}

# Layer 2: private browser window title patterns
_PRIVATE_WINDOW_PATTERNS = [
    re.compile(r"private browsing", re.IGNORECASE),
    re.compile(r"incognito", re.IGNORECASE),
    re.compile(r"inprivate", re.IGNORECASE),           # Edge
    re.compile(r"private window", re.IGNORECASE),       # Firefox
]


# ---------------------------------------------------------------------------
# Layer 1: App blocklist
# ---------------------------------------------------------------------------

def is_app_blocked(app_name):
    """Check if an app is on the blocklist."""
    return app_name.lower() in BLOCKED_APPS


# ---------------------------------------------------------------------------
# Layer 2: Window state filtering
# ---------------------------------------------------------------------------

def is_private_window(app_name, window_title, ax_window=None):
    """
    Detect private/incognito browser windows.

    Checks three signals:
    1. Window title heuristics (Chrome, Safari, Firefox, Edge)
    2. AXIdentifier on the window element (Arc uses "bigIncognitoBrowserWindow")
    3. AXSubrole or AXDescription containing private/incognito hints
    """
    # Check title
    for pattern in _PRIVATE_WINDOW_PATTERNS:
        if pattern.search(window_title):
            return True

    # Check AX window element properties (catches Arc incognito)
    if ax_window is not None:
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue

            for attr in ("AXIdentifier", "AXDescription", "AXSubrole"):
                err, val = AXUIElementCopyAttributeValue(ax_window, attr, None)
                if err == 0 and isinstance(val, str):
                    val_lower = val.lower()
                    if "incognito" in val_lower or "private" in val_lower:
                        return True
        except Exception:
            pass

    return False


def is_app_hidden(pid):
    """
    Check if the app is hidden (minimized / not visible).

    Uses NSRunningApplication to check the isHidden property.
    """
    try:
        from AppKit import NSWorkspace
        workspace = NSWorkspace.sharedWorkspace()
        for app in workspace.runningApplications():
            if app.processIdentifier() == pid:
                return app.isHidden()
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Layer 3: AX secure field detection
# ---------------------------------------------------------------------------

def is_secure_field(element):
    """
    Check if an AX element is a secure text field (password input).

    Returns True if the element's AXSubrole is in the blocked set.
    """
    from ApplicationServices import AXUIElementCopyAttributeValue

    err, subrole = AXUIElementCopyAttributeValue(element, "AXSubrole", None)
    if err == 0 and isinstance(subrole, str):
        return subrole in BLOCKED_AX_SUBROLES
    return False


# ---------------------------------------------------------------------------
# Layer 4: URL parameter scrubbing
# ---------------------------------------------------------------------------

def scrub_url(url):
    """
    Strip sensitive query parameters from a URL.

    Parameters whose key matches any entry in SENSITIVE_URL_KEYS
    (case-insensitive) are replaced with [REDACTED].

    Example:
        https://example.com/auth?token=abc123&page=1
        → https://example.com/auth?token=[REDACTED]&page=1
    """
    if not SENSITIVE_URL_KEYS:
        return url

    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url

        pairs = []
        for part in parsed.query.split("&"):
            if "=" in part:
                key, _val = part.split("=", 1)
                if key.lower() in SENSITIVE_URL_KEYS:
                    pairs.append(f"{key}=[REDACTED]")
                else:
                    pairs.append(part)
            else:
                pairs.append(part)

        new_query = "&".join(pairs)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url


def scrub_text_urls(text):
    """
    Find URLs embedded in text and scrub their sensitive parameters.

    Scans for http/https URLs in the text string and applies
    scrub_url to each one.
    """
    if not SENSITIVE_URL_KEYS:
        return text

    url_pattern = re.compile(r'https?://[^\s<>"\']+')

    def _replace(match):
        return scrub_url(match.group(0))

    return url_pattern.sub(_replace, text)


# ---------------------------------------------------------------------------
# Layer 5: Sensitive page / context detection
# ---------------------------------------------------------------------------

# Window titles that indicate a sensitive page (password manager, banking, etc.)
_SENSITIVE_TITLE_PATTERNS = [
    re.compile(r"password\s*manager", re.IGNORECASE),
    re.compile(r"passwords?\s*[-—]", re.IGNORECASE),       # "Passwords -" or "Password —"
    re.compile(r"saved\s*passwords?", re.IGNORECASE),
    re.compile(r"keychain", re.IGNORECASE),
    re.compile(r"credentials?", re.IGNORECASE),
    re.compile(r"autofill", re.IGNORECASE),
    re.compile(r"credit\s*card", re.IGNORECASE),
    re.compile(r"payment\s*method", re.IGNORECASE),
    re.compile(r"billing\s*info", re.IGNORECASE),
    re.compile(r"bank\s*account", re.IGNORECASE),
]

# Content patterns that strongly indicate a password/credential display.
# These check the captured text body, not the title.
_SENSITIVE_CONTENT_PATTERNS = [
    re.compile(r"copy\s*password", re.IGNORECASE),
    re.compile(r"hide\s*password", re.IGNORECASE),
    re.compile(r"show\s*password", re.IGNORECASE),
    re.compile(r"edit\s*password", re.IGNORECASE),
    re.compile(r"delete\s*password", re.IGNORECASE),
    re.compile(r"copy\s*username", re.IGNORECASE),
    re.compile(r"search\s*passwords", re.IGNORECASE),
    re.compile(r"card\s*number", re.IGNORECASE),
    re.compile(r"expir(y|ation)\s*date", re.IGNORECASE),
    re.compile(r"security\s*code", re.IGNORECASE),
    re.compile(r"cvv", re.IGNORECASE),
    re.compile(r"routing\s*number", re.IGNORECASE),
    re.compile(r"account\s*number", re.IGNORECASE),
]

# Custom patterns from config
_custom_title = _PRIVACY_CFG.get("sensitive_title_patterns", [])
_SENSITIVE_TITLE_PATTERNS.extend(re.compile(p, re.IGNORECASE) for p in _custom_title)

_custom_content = _PRIVACY_CFG.get("sensitive_content_patterns", [])
_SENSITIVE_CONTENT_PATTERNS.extend(re.compile(p, re.IGNORECASE) for p in _custom_content)


def is_sensitive_page(window_title, text):
    """
    Detect if the current page/window shows sensitive content
    like a password manager, banking page, or payment form.

    Checks both the window title and the captured text body.
    Returns True if the page should be blocked.
    """
    for pattern in _SENSITIVE_TITLE_PATTERNS:
        if pattern.search(window_title):
            return True

    for pattern in _SENSITIVE_CONTENT_PATTERNS:
        if pattern.search(text):
            return True

    return False

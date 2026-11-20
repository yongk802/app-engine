"""App Review: the admission gate every app passes before app-engine serves it.

A review has two halves, both static (app code is never executed):

* **Contract** — does the folder implement what app-engine needs to host it:
  a valid manifest, a launch surface, the assets the manifest names, the
  permissions the code actually uses, and (for store releases) complete
  listing metadata.
* **Security** — an OWASP Top 10 (2021) mapped scan of every file the app
  ships: secrets, DOM/OS/SQL injection sinks, disabled TLS, unpinned or
  unverified remote code, known-vulnerable vendored libraries, native
  binaries, attempts to reach the host's admin surface or another app's
  state, and so on.

A finding of severity ``critical`` or ``high`` blocks admission unless the
owner has waived that exact finding. Waivers live in the engine's state
directory, never in the app, so an app cannot approve itself.

Run it from a shell before shipping an app::

    python -m app_engine.app_review path/to/app [--profile store] [--json]
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time

from . import __version__
from .manifest import parse_manifest

REVIEW_VERSION = 1
SEVERITIES = ("critical", "high", "medium", "low", "info")
BLOCKING = frozenset({"critical", "high"})
PROFILES = ("local", "store")
_WEIGHT = {"critical": 40, "high": 20, "medium": 5, "low": 1, "info": 0}

MAX_FILES = 20000
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_TOTAL_BYTES = 96 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_FINDINGS_PER_RULE = 25

TEXT_SUFFIXES = frozenset({
    ".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".json",
    ".py", ".sh", ".bash", ".zsh", ".ps1", ".rb", ".go", ".php", ".txt", ".md",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".svg",
    ".vue", ".svelte", ".lock", ".csv",
})
CODE_SUFFIXES = frozenset({".html", ".htm", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
                           ".vue", ".svelte"})
NATIVE_SUFFIXES = frozenset({".exe", ".dll", ".so", ".dylib", ".bin", ".com", ".scr",
                             ".msi", ".app", ".jar", ".class", ".node"})
MEDIA_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".wav", ".mp3", ".ogg",
                            ".mp4", ".webm", ".ttf", ".otf", ".woff", ".woff2", ".wasm"})
NATIVE_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe")
OWASP = {
    "A01": "A01:2021 Broken Access Control",
    "A02": "A02:2021 Cryptographic Failures",
    "A03": "A03:2021 Injection",
    "A04": "A04:2021 Insecure Design",
    "A05": "A05:2021 Security Misconfiguration",
    "A06": "A06:2021 Vulnerable and Outdated Components",
    "A07": "A07:2021 Identification and Authentication Failures",
    "A08": "A08:2021 Software and Data Integrity Failures",
    "A09": "A09:2021 Security Logging and Monitoring Failures",
    "A10": "A10:2021 Server-Side Request Forgery",
    "contract": "App contract",
}
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")
# Not reviewed: never packaged by the app store (dev/runtime output, owner-only
# private/), or created by the engine's own runtime install steps.
SKIPPED_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
                          ".pytest_cache", ".mypy_cache", ".ruff_cache", ".runtime", "private"})
# Credential directories are never packaged either, but their presence is reported.
FLAGGED_DIRS = frozenset({".ssh", ".aws", ".gnupg"})
TEST_PATH = re.compile(r"(?i)(?:^|/)(?:tests?|__tests__|spec|specs|e2e|browser_tests?|fixtures)/|"
                       r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.py|[^/]+\.(?:test|spec)\.[cm]?[jt]sx?|tests?\.[cm]?js)$")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    category: str
    title: str
    detail: str
    remediation: str = ""
    path: str = ""
    line: int = 0
    excerpt: str = ""
    fingerprint: str = ""
    waivable: bool = True
    waived: bool = False
    waiver_reason: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING and not self.waived


@dataclass(frozen=True)
class ReviewReport:
    app_id: str
    label: str
    version: str
    root: str
    profile: str
    signature: str
    scanned_at: str
    engine_version: str
    review_version: int
    files_scanned: int
    bytes_scanned: int
    findings: tuple[Finding, ...] = ()

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.blocking)

    @property
    def passed(self) -> bool:
        return not self.blocking

    @property
    def score(self) -> int:
        per_rule: dict[str, int] = {}
        for item in self.findings:
            if not item.waived:
                per_rule[item.rule] = per_rule.get(item.rule, 0) + _WEIGHT[item.severity]
        # One noisy rule costs at most twice its single-finding weight.
        caps = {item.rule: 2 * _WEIGHT[item.severity] for item in self.findings}
        return max(0, 100 - sum(min(cost, caps[rule]) for rule, cost in per_rule.items()))

    def counts(self) -> dict[str, int]:
        out = {name: 0 for name in SEVERITIES}
        for item in self.findings:
            if not item.waived:
                out[item.severity] += 1
        out["waived"] = sum(1 for item in self.findings if item.waived)
        return out

    def summary(self) -> str:
        blocking = self.blocking
        if not blocking:
            return "passed"
        first = "; ".join(f"{item.rule} {item.title}" + (f" ({item.path})" if item.path else "")
                          for item in blocking[:3])
        more = f" and {len(blocking) - 3} more" if len(blocking) > 3 else ""
        return f"{len(blocking)} blocking finding{'s' if len(blocking) != 1 else ''}: {first}{more}"

    def to_dict(self) -> dict:
        value = asdict(self)
        value["findings"] = [dict(asdict(item), blocking=item.blocking) for item in self.findings]
        value.update(verdict="pass" if self.passed else "fail", score=self.score,
                     counts=self.counts(), summary=self.summary())
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "ReviewReport":
        names = {f for f in Finding.__dataclass_fields__}
        findings = tuple(Finding(**{k: v for k, v in item.items() if k in names})
                         for item in value.get("findings", ()))
        names = {f for f in cls.__dataclass_fields__} - {"findings"}
        return cls(**{k: v for k, v in value.items() if k in names}, findings=findings)

    def with_waivers(self, waivers: dict[str, str]) -> "ReviewReport":
        findings = tuple(
            replace(item, waived=item.waivable and item.fingerprint in waivers,
                    waiver_reason=waivers.get(item.fingerprint, "") if item.waivable else "")
            for item in self.findings)
        return replace(self, findings=findings)


# ── Rules ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    rule: str
    severity: str
    owasp: str
    title: str
    detail: str
    remediation: str
    pattern: re.Pattern
    suffixes: frozenset[str] = frozenset()   # empty = every text file
    minified_ok: bool = True                  # also run on minified/vendored bundles
    secret: bool = False                      # redact the match in the excerpt
    unless_line: re.Pattern | None = None     # skip the match when the line matches
    code_only: bool = False                   # ignore matches inside string literals / prose


def _r(pattern: str, flags: int = 0) -> re.Pattern:
    return re.compile(pattern, flags)


JS = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".html", ".htm", ".vue", ".svelte"})
PY = frozenset({".py"})
SHELL = frozenset({".sh", ".bash", ".zsh", ".ps1", ".md", ".txt", ".py", ".yml", ".yaml",
                   ".json", ".toml"})
HTML = frozenset({".html", ".htm"})
SERVER = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".rb", ".php"})
PLACEHOLDER = _r(r"(?i)(example|placeholder|your[-_ ]?|changeme|xxxx|\*\*\*\*|<[^>]+>|\$\{|process\.env|os\.environ|getenv|dummy|test[-_]?key|redacted)")

RULES: tuple[Rule, ...] = (
    # A02 / A07 — secrets shipped inside the app
    Rule("SEC-PRIVATE-KEY", "critical", "A02", "Private key in app files",
         "A PEM private key ships with the app; anyone who can load the app can read it.",
         "Remove the key, rotate it, and load keys from the host's configuration instead.",
         _r(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"), secret=True),
    Rule("SEC-AWS-KEY", "critical", "A07", "AWS access key",
         "An AWS access key id is embedded in the app.", "Remove and rotate the key.",
         _r(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), secret=True),
    Rule("SEC-GITHUB-TOKEN", "critical", "A07", "GitHub token",
         "A GitHub token is embedded in the app.", "Remove and revoke the token.",
         _r(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"), secret=True),
    Rule("SEC-SLACK-TOKEN", "critical", "A07", "Slack token",
         "A Slack token is embedded in the app.", "Remove and revoke the token.",
         _r(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), secret=True),
    Rule("SEC-AI-KEY", "critical", "A07", "AI provider API key",
         "An OpenAI/Anthropic style API key is embedded in the app.",
         "Remove and revoke the key; route model calls through the engine's Local AI or a backend that holds the key.",
         _r(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}\b"), secret=True),
    Rule("SEC-STRIPE-KEY", "critical", "A07", "Stripe live secret key",
         "A live Stripe secret key is embedded in the app.", "Remove and roll the key.",
         _r(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b"), secret=True),
    Rule("SEC-GOOGLE-KEY", "high", "A07", "Google API key",
         "A Google API key is embedded in the app.",
         "Remove it, or restrict the key by referrer/API and document why it must ship.",
         _r(r"\bAIza[0-9A-Za-z_-]{35}\b"), secret=True),
    Rule("SEC-GENERIC-SECRET", "medium", "A07", "Hard-coded credential",
         "A value that looks like a password, token or API key is assigned in source.",
         "Load credentials from configuration at runtime; never ship them in app files.",
         _r(r"""(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|passw(?:or)?d|access[_-]?token|auth[_-]?token|client[_-]?secret)["']?\s*[:=]\s*["']([^"'\s]{12,})["']"""),
         suffixes=frozenset(TEXT_SUFFIXES - {".md", ".txt", ".csv"}), minified_ok=False,
         secret=True, unless_line=PLACEHOLDER),
    Rule("SEC-JWT", "medium", "A07", "Embedded JSON Web Token",
         "A signed JWT is embedded in the app; it may grant access to someone's account.",
         "Remove the token.",
         _r(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), secret=True),

    # A03 — injection sinks
    Rule("INJ-EVAL", "high", "A03", "Dynamic code evaluation (eval)",
         "eval() runs a string as code; any attacker-influenced text becomes script.",
         "Parse data with JSON.parse or a real parser instead of evaluating it.",
         _r(r"(?<![\w.$])eval\s*\("), suffixes=JS, minified_ok=False,
         unless_line=_r(r"^\s*(?://|\*|/\*)"), code_only=True),
    Rule("INJ-NEW-FUNCTION", "high", "A03", "Dynamic code construction (new Function)",
         "new Function() compiles a string into code, the same risk as eval().",
         "Replace with ordinary functions or a data-driven interpreter.",
         _r(r"\bnew\s+Function\s*\("), suffixes=JS, minified_ok=False,
         unless_line=_r(r"^\s*(?://|\*|/\*)"), code_only=True),
    Rule("INJ-STRING-TIMER", "medium", "A03", "String passed to setTimeout/setInterval",
         "Timers given a string evaluate it as code.", "Pass a function instead of a string.",
         _r(r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]"), suffixes=JS, minified_ok=False, code_only=True),
    Rule("INJ-DOC-WRITE", "medium", "A03", "document.write",
         "document.write injects raw HTML and is a common DOM-XSS sink.",
         "Build nodes with createElement/textContent.",
         _r(r"\bdocument\.write(?:ln)?\s*\("), suffixes=JS, minified_ok=False, code_only=True),
    Rule("INJ-HTML-SINK", "medium", "A03", "Raw HTML sink with dynamic content",
         "innerHTML/outerHTML/insertAdjacentHTML with a non-literal value can execute injected markup.",
         "Use textContent, or sanitize with a vetted sanitizer before assignment.",
         _r(r"""(?:\.(?:inner|outer)HTML\s*\+?=\s*(?![\s]*(?:["'][^"'`$]*["']\s*;?\s*$|``\s*;?\s*$|$))|\.insertAdjacentHTML\s*\(\s*["'][a-z]+["']\s*,\s*(?!["'][^"']*["']\s*\)))"""),
         suffixes=JS, minified_ok=False),
    Rule("INJ-JS-URL", "medium", "A03", "javascript: URL",
         "javascript: URLs execute code on navigation and bypass event-handler CSP.",
         "Attach event listeners instead.",
         _r(r"""(?i)(?:href|src|action)\s*=\s*["']\s*javascript:"""), suffixes=JS, minified_ok=False),
    Rule("INJ-SHELL", "high", "A03", "OS command injection risk",
         "Commands run through a shell; interpolated input can run arbitrary commands.",
         "Use subprocess with an argument list and shell=False.",
         _r(r"\bsubprocess\.\w+\([^\n]*shell\s*=\s*True|\bos\.(?:system|popen)\s*\("), suffixes=PY, code_only=True),
    Rule("INJ-NODE-EXEC", "high", "A03", "Node shell execution",
         "child_process.exec/execSync run through a shell.",
         "Use execFile/spawn with an argument array.",
         _r(r"\b(?:child_process|cp)\.exec(?:Sync)?\s*\(|\bexecSync\s*\("), suffixes=frozenset({".js", ".mjs", ".cjs", ".ts"}),
         minified_ok=False, code_only=True),
    Rule("INJ-PY-EVAL", "high", "A03", "Python eval/exec",
         "eval()/exec() run strings as Python code.",
         "Use ast.literal_eval or a real parser.",
         _r(r"(?<![\w.])(?:eval|exec)\s*\((?!\s*\))"), suffixes=PY,
         unless_line=_r(r"^\s*#|literal_eval"), code_only=True),
    Rule("INJ-SQL", "high", "A03", "SQL built from string formatting",
         "SQL text built with f-strings, % or .format() is injectable.",
         "Use parameterized queries (placeholders + parameters).",
         _r(r"""\.execute(?:many|script)?\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\.format\s*\()|[^,)]*\+\s*\w)"""),
         suffixes=PY, unless_line=_r(r"(?i)execute\w*\s*\(\s*f?[\"'](?:ALTER|CREATE|DROP|PRAGMA)\b")),

    # A02 — transport and crypto
    Rule("TLS-VERIFY-OFF", "high", "A02", "TLS certificate verification disabled",
         "Disabling certificate checks lets anyone on the network impersonate the server.",
         "Keep verification on; trust a private CA explicitly if needed.",
         _r(r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED|_create_unverified_context|CERT_NONE|InsecureSkipVerify\s*:\s*true"),
         suffixes=SERVER | JS),
    Rule("CRYPTO-WEAK-RANDOM", "medium", "A02", "Math.random used for a security value",
         "Math.random is predictable and must not produce tokens, ids or keys.",
         "Use crypto.getRandomValues / crypto.randomUUID.",
         _r(r"(?i)(?:token|secret|password|nonce|session|api[_-]?key)[^\n]{0,60}Math\.random\(|Math\.random\(\)[^\n]{0,60}(?:token|secret|password|nonce|session)"),
         suffixes=JS, minified_ok=False),
    Rule("NET-CLEARTEXT", "medium", "A02", "Cleartext network request",
         "Requests over http:// or ws:// to a remote host can be read and altered in transit.",
         "Use https:// / wss://.",
         _r(r"""(?:fetch|WebSocket|EventSource|open)\s*\(\s*(?:["'`])(?:http|ws)://(?!localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)"""),
         suffixes=JS, minified_ok=False),

    # A05 — misconfiguration
    Rule("CFG-CORS-ANY", "medium", "A05", "CORS allows any origin",
         "A backend that answers every origin lets any web page call it from the user's browser.",
         "Allow only the app's own origin.",
         _r(r"""Access-Control-Allow-Origin["']?\s*[:,]\s*["']\*|allow_origins\s*=\s*\[\s*["']\*|origin\s*:\s*["']\*["']"""),
         suffixes=SERVER),
    Rule("CFG-BIND-ALL", "medium", "A05", "Backend listens on every interface",
         "Binding 0.0.0.0 exposes the app backend to the whole network, outside the engine's gateway.",
         "Bind 127.0.0.1; the engine proxies loopback backends.",
         _r(r"""host\s*=\s*["']0\.0\.0\.0["']|--host[ =]0\.0\.0\.0|listen\([^)]*["']0\.0\.0\.0["']"""),
         suffixes=SERVER | SHELL),
    Rule("CFG-DEBUG", "medium", "A05", "Debug mode enabled",
         "Debug servers expose tracebacks and sometimes an interactive console.",
         "Turn debug off for shipped apps.",
         _r(r"\.run\([^\n]*debug\s*=\s*True|DEBUG\s*=\s*True"), suffixes=PY),

    # A08 — integrity of code and data
    Rule("INT-CURL-PIPE", "high", "A08", "Remote script piped into a shell",
         "curl|sh runs whatever the server returns at that moment, unreviewed.",
         "Download to a file, verify a pinned checksum, then run it.",
         _r(r"(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b|\biex\s*\(\s*(?:iwr|Invoke-WebRequest|New-Object\s+Net\.WebClient)"),
         suffixes=SHELL),
    Rule("INT-PICKLE", "high", "A08", "Unsafe deserialization",
         "pickle/marshal/yaml.load on untrusted data executes code.",
         "Use JSON, or yaml.safe_load.",
         _r(r"\b(?:pickle|cPickle|marshal|dill)\.loads?\s*\(|\byaml\.load\s*\((?![^)]*SafeLoader)"), suffixes=PY, code_only=True),
    Rule("INT-REMOTE-IMPORT", "high", "A08", "Unpinned code imported from a remote URL",
         "The module URL names no exact version, so the code that runs can change after review.",
         "Pin an exact version (pkg@1.2.3), or better, vendor the module into the app.",
         _r(r"""(?:\bimport\s*\(\s*|\bimportScripts\s*\(\s*|^\s*import\s[^'"\n]*from\s*)["'`](https?://(?![^"'`]*@\d)[^"'`]+)"""),
         suffixes=JS, minified_ok=False),
    Rule("INT-REMOTE-IMPORT-PINNED", "low", "A08", "Pinned code imported from a remote URL",
         "A version-pinned module still loads from the network at runtime (no integrity check for ES imports).",
         "Vendor the module so the reviewed bytes are the bytes that run, and the app works offline.",
         _r(r"""(?:\bimport\s*\(\s*|\bimportScripts\s*\(\s*|^\s*import\s[^'"\n]*from\s*)["'`](https?://[^"'`]*@\d[^"'`]*)"""),
         suffixes=JS, minified_ok=False),

    # A01 — reaching past the app's own boundary
    Rule("ACL-HOST-ADMIN", "high", "A01", "Calls the engine's owner-only API",
         "The app references owner/admin endpoints or the launcher capability header.",
         "Apps must use only their own app-state and the documented app APIs.",
         _r(r"""x-app-engine-admin|/api/app-engine/(?:stores|store-installs|review|assistant)|/api/local-ai/(?:profile|authorize|install|start|pull|verify)|/admin/sign-"""),
         suffixes=JS | SERVER, minified_ok=False),
    Rule("ACL-FRAME-ESCAPE", "high", "A01", "Reaches into the launcher frame",
         "Accessing parent/top documents or storage tries to escape the app's iframe.",
         "Communicate with postMessage and validate origins.",
         _r(r"\b(?:window\.)?(?:parent|top)\.(?:document|localStorage|sessionStorage|frames|location\.href\s*=)"),
         suffixes=JS, minified_ok=False),
    Rule("ACL-POSTMESSAGE-ANY", "medium", "A01", "postMessage to any origin",
         "Posting with targetOrigin '*' sends data to whatever page is embedding or embedded.",
         "Pass the exact target origin.",
         _r(r"""postMessage\s*\([^;\n]*,\s*["']\*["']"""), suffixes=JS, minified_ok=False),
)

VULNERABLE_LIBRARIES = (
    # (name, detector, fixed-in, severity, advisory)
    ("jquery", _r(r"(?i)jquery[.@/-]v?(\d+\.\d+\.\d+)|jQuery (?:JavaScript Library )?v(\d+\.\d+\.\d+)"), (3, 5, 0), "high",
     "CVE-2020-11022/11023: XSS in HTML manipulation"),
    ("lodash", _r(r"(?i)lodash[.@/-](\d+\.\d+\.\d+)|@license\s+Lodash[^\n]*?(\d+\.\d+\.\d+)"), (4, 17, 21), "high",
     "CVE-2021-23337/CVE-2020-8203: command injection and prototype pollution"),
    ("angularjs", _r(r"(?i)angular(?:js)?[.@/-](1\.\d+\.\d+)|AngularJS v(1\.\d+\.\d+)"), (2, 0, 0), "high",
     "AngularJS 1.x is end-of-life with unpatched sandbox-escape XSS"),
    ("handlebars", _r(r"(?i)handlebars[.@/-]v?(\d+\.\d+\.\d+)"), (4, 7, 7), "high",
     "CVE-2021-23369: remote code execution in templates"),
    ("bootstrap", _r(r"(?i)bootstrap[.@/-]v?(\d+\.\d+\.\d+)|Bootstrap v(\d+\.\d+\.\d+)"), (3, 4, 1), "medium",
     "CVE-2019-8331: XSS in tooltip/popover"),
    ("moment", _r(r"(?i)moment[.@/-](\d+\.\d+\.\d+)|//! moment\.js[^\n]*?(\d+\.\d+\.\d+)"), (2, 29, 4), "medium",
     "CVE-2022-31129: ReDoS in RFC 2822 parsing"),
    ("dompurify", _r(r"(?i)dompurify[.@/-](\d+\.\d+\.\d+)|DOMPurify (\d+\.\d+\.\d+)"), (2, 4, 2), "high",
     "Mutation-XSS bypasses fixed in 2.4.2+"),
    ("marked", _r(r"(?i)marked[.@/-]v?(\d+\.\d+\.\d+)"), (4, 0, 10), "medium",
     "CVE-2022-21680/21681: ReDoS"),
)
_BOOTSTRAP4_FIXED = (4, 3, 1)

EXTERNAL_TAG = _r(r"""(?is)<(script|link)\b([^>]*?)\b(src|href)\s*=\s*["']((?:https?:)?//[^"']+)["']([^>]*)>""")
CDN_HOSTS = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "esm.sh", "cdn.skypack.dev",
             "ga.jspm.io", "code.jquery.com", "cdn.tailwindcss.com", "esm.run")
LOCAL_REF = _r(r"""(?i)<(?:script|img|link|audio|video|source|iframe)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"'#?]+)""")
APP_STATE_REF = _r(r"""/api/app-state/([a-z0-9][a-z0-9-]*)""")
USES_PERMISSION = {
    "microphone": _r(r"getUserMedia\s*\([^)]*audio|SpeechRecognition|webkitSpeechRecognition"),
    "camera": _r(r"getUserMedia\s*\([^)]*video"),
    "display-capture": _r(r"getDisplayMedia\s*\("),
    "geolocation": _r(r"navigator\.geolocation\."),
    "midi": _r(r"requestMIDIAccess\s*\("),
    "clipboard-read": _r(r"clipboard\.read(?:Text)?\s*\("),
    "clipboard-write": _r(r"clipboard\.write(?:Text)?\s*\("),
    "fullscreen": _r(r"requestFullscreen\s*\(|webkitRequestFullscreen"),
}
# Permissions the browser grants without a Permissions-Policy delegation are
# not reported as "used but undeclared".
_UNDECLARED_OK = frozenset({"clipboard-write", "autoplay"})
SANDBOX_RISK = {
    "allow-top-navigation": ("high", "lets the app navigate the launcher away (phishing / clickjacking)"),
    "allow-top-navigation-by-user-activation": ("medium", "lets the app navigate the launcher after a click"),
    "allow-popups-to-escape-sandbox": ("medium", "opened windows run without the app's sandbox"),
}
SENSITIVE_FILES = (
    (_r(r"(?i)(?:^|/)\.env(?:\.[\w-]+)?$"), "critical", ".env file"),
    (_r(r"(?i)(?:^|/)(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)$"), "critical", "SSH private key"),
    (_r(r"(?i)\.(?:pem|key|p12|pfx|jks|keystore)$"), "high", "key or certificate store"),
    (_r(r"(?i)(?:^|/)(?:\.npmrc|\.pypirc|\.netrc|\.git-credentials|credentials(?:\.json)?|secrets?\.(?:json|ya?ml|toml))$"),
     "high", "credential file"),
    (_r(r"(?i)\.(?:sqlite3?|db)$"), "medium", "database file"),
    (_r(r"(?i)(?:^|/)\.(?:git|svn|hg)(?:/|$)"), "high", "version-control metadata"),
    (_r(r"(?i)(?:^|/)\.DS_Store$"), "low", "macOS Finder metadata"),
    (_r(r"(?i)\.(?:bak|old|orig|swp)$|~$"), "low", "editor or backup file"),
)


# ── Scanning ─────────────────────────────────────────────────────────────────

def _fingerprint(rule: str, path: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip())[:200]
    return hashlib.sha256(f"{rule}\0{path}\0{norm}".encode()).hexdigest()[:20]


def _redact(text: str) -> str:
    text = text.strip()
    return (text[:4] + "…" + f"[{len(text)} chars redacted]") if len(text) > 4 else "…"


def _excerpt(line: str, start: int, end: int) -> str:
    lo = max(0, start - 60)
    hi = min(len(line), end + 60)
    return (("…" if lo else "") + line[lo:hi].strip() + ("…" if hi < len(line) else ""))[:240]


def _in_string(line: str, index: int) -> bool:
    """True when ``index`` sits inside a quoted literal or HTML prose on this line."""
    quote = ""
    escaped = False
    for char in line[:index]:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
    return bool(quote) or bool(re.search(r"<(?:code|pre|p|li|td|span)\b[^>]*>[^<]*$", line[:index]))


def _is_minified(path: str, text: str) -> bool:
    if re.search(r"(?i)\.min\.(?:js|mjs|css)$|(?:^|/)(?:vendor|vendors|node_modules|lib|libs|third[_-]?party|dist)/", path):
        return True
    lines = text.count("\n") + 1
    return len(text) > 20000 and len(text) / lines > 300


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    try:
        parts = tuple(int(p) for p in text.split(".")[:3])
    except ValueError:
        return None
    return (parts + (0, 0, 0))[:3]  # type: ignore[return-value]


class _Collector:
    def __init__(self) -> None:
        self.items: list[Finding] = []
        self._per_rule: dict[str, int] = {}

    def add(self, rule: str, severity: str, owasp: str, title: str, detail: str,
            remediation: str = "", path: str = "", line: int = 0, excerpt: str = "",
            key: str = "", waivable: bool = True) -> None:
        if path and TEST_PATH.search(path) and severity in BLOCKING and not rule.startswith(("FS-", "CONTRACT-")):
            # Test code is not served or run by the engine; keep it visible, not blocking.
            severity = "medium" if rule.startswith("SEC-") else "low"
            detail += " (test file)"
        count = self._per_rule.get(rule, 0)
        self._per_rule[rule] = count + 1
        if count >= MAX_FINDINGS_PER_RULE:
            return
        self.items.append(Finding(
            rule=rule, severity=severity, category=OWASP[owasp], title=title, detail=detail,
            remediation=remediation, path=path, line=line, excerpt=excerpt,
            fingerprint=_fingerprint(rule, path, key or excerpt or detail), waivable=waivable))

    def finish(self) -> tuple[Finding, ...]:
        for rule, count in self._per_rule.items():
            if count > MAX_FINDINGS_PER_RULE:
                self.items.append(Finding(
                    rule=rule, severity="info", category=OWASP["A09"],
                    title=f"{count - MAX_FINDINGS_PER_RULE} more {rule} findings not listed",
                    detail="Only the first matches of each rule are reported.",
                    fingerprint=_fingerprint(rule, "", "overflow"), waivable=False))
        order = {name: index for index, name in enumerate(SEVERITIES)}
        return tuple(sorted(self.items, key=lambda f: (order[f.severity], f.rule, f.path, f.line)))


def _walk(root: Path):
    """Yield (relative posix path, absolute path, lstat) without following links."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in SKIPPED_DIRS)
        base = Path(directory)
        for name in sorted(dirs):
            full = base / name
            if full.is_symlink() or name in FLAGGED_DIRS:
                yield full.relative_to(root).as_posix(), full, full.lstat()
        for name in sorted(files):
            full = base / name
            try:
                yield full.relative_to(root).as_posix(), full, full.lstat()
            except OSError:
                continue


# Any change to the scanner itself invalidates every cached report.
_SCANNER_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def tree_signature(root: Path) -> str:
    """Cheap change detector: paths, sizes, mtimes and modes, never contents."""
    digest = hashlib.sha256(f"review-v{REVIEW_VERSION}-{_SCANNER_DIGEST}".encode())
    try:
        for rel, _, info in _walk(Path(root)):
            digest.update(f"{rel}\0{info.st_size}\0{info.st_mtime_ns}\0{info.st_mode}\n".encode())
    except OSError:
        digest.update(b"unreadable")
    return digest.hexdigest()


def _read_raw(root: Path) -> dict:
    try:
        loaded = json.loads((root / "app.json").read_text("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


@dataclass
class _FileScan:
    root: Path
    app_id: str
    out: _Collector
    files: int = 0
    text_bytes: int = 0
    total_bytes: int = 0
    truncated: bool = False
    texts: dict[str, str] = field(default_factory=dict)


def review_app(root: Path, *, profile: str = "local") -> ReviewReport:
    """Review one app directory. Never raises on app content."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    root = Path(root).resolve()
    out = _Collector()
    signature = tree_signature(root)
    inspection = parse_manifest(root)
    manifest = inspection.manifest
    raw = _read_raw(root)
    app_id = str(manifest.app_id) if manifest else str(raw.get("id") or root.name)

    for issue in inspection.errors:
        out.add("CONTRACT-MANIFEST", "critical", "contract", "Manifest is invalid", issue.message,
                "Fix app.json; run `python -m app_engine.manifest <dir>` for details.",
                path="app.json", key=issue.code + issue.path, waivable=False)
    for issue in inspection.warnings:
        out.add("CONTRACT-MANIFEST-WARNING", "low", "contract", "Manifest warning", issue.message,
                path="app.json", key=issue.message)
    if manifest is not None:
        _contract(root, raw, manifest, profile, out)

    scan = _FileScan(root, app_id, out)
    for rel, full, info in _walk(root):
        scan.files += 1
        if scan.files > MAX_FILES:
            scan.truncated = True
            break
        _scan_entry(scan, rel, full, info)
    _scan_totals(scan, profile)
    if manifest is not None:
        _permission_usage(manifest, scan.texts, out)
        _dependencies(root, scan.texts, out)

    meta = manifest.metadata if manifest else None
    return ReviewReport(
        app_id=app_id, label=manifest.label if manifest else str(raw.get("label") or app_id),
        version=meta.version if meta else str(raw.get("version") or ""), root=str(root),
        profile=profile, signature=signature,
        scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        engine_version=__version__, review_version=REVIEW_VERSION,
        files_scanned=min(scan.files, MAX_FILES), bytes_scanned=scan.text_bytes,
        findings=out.finish())


def _scan_totals(scan: _FileScan, profile: str) -> None:
    if scan.truncated:
        scan.out.add("SCAN-TRUNCATED", "medium", "A09", "Review was incomplete",
                     f"The app exceeds review limits ({MAX_FILES} files / {MAX_TEXT_TOTAL_BYTES // 2**20} MiB of text).",
                     "Remove unneeded files so the whole app can be reviewed.")
    if profile == "store" and scan.total_bytes > MAX_PACKAGE_BYTES:
        scan.out.add("CONTRACT-SIZE", "high", "contract", "App exceeds the package size limit",
                     f"{scan.total_bytes // 2**20} MiB > {MAX_PACKAGE_BYTES // 2**20} MiB.",
                     "Remove unused assets or move them under private/.")


def _scan_link(scan: _FileScan, rel: str, full: Path) -> None:
    try:
        target = full.resolve()
        inside = target == scan.root or scan.root in target.parents
    except OSError:
        inside = False
    if inside:
        scan.out.add("FS-SYMLINK", "low", "A05", "Symbolic link inside the app",
                     "Links make the reviewed tree differ from what gets packaged.",
                     "Replace the link with the file it points to.", path=rel)
    else:
        scan.out.add("FS-SYMLINK-ESCAPE", "high", "A01", "Symbolic link leaves the app folder",
                     "The link exposes files outside the app to anything that serves or packages it.",
                     "Remove the link and copy the needed file into the app.", path=rel)


def _is_native(full: Path, suffix: str) -> bool:
    if suffix in NATIVE_SUFFIXES:
        return True
    if suffix in MEDIA_SUFFIXES:
        return False
    try:
        with full.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    return any(head.startswith(magic) for magic in NATIVE_MAGIC)


def _file_hygiene(scan: _FileScan, rel: str, info: os.stat_result) -> None:
    if info.st_mode & 0o002:
        scan.out.add("FS-WORLD-WRITABLE", "medium", "A05", "World-writable file",
                     "Any local user can change this file after review.", "chmod o-w the file.", path=rel)
    for pattern, severity, label in SENSITIVE_FILES:
        if pattern.search(rel):
            scan.out.add("FS-SENSITIVE-FILE", severity, "A05" if severity == "low" else "A02",
                         f"Ships a {label}", f"{rel} should not be distributed with an app.",
                         "Delete it from the app folder (or move it under private/ outside the package).",
                         path=rel)
            break
    if info.st_size > 50 * 1024 * 1024:
        scan.out.add("FS-LARGE-FILE", "low", "A04", "Very large file",
                     f"{info.st_size // (1024 * 1024)} MiB; large assets slow installs and reviews.", path=rel)


def _scan_entry(scan: _FileScan, rel: str, full: Path, info: os.stat_result) -> None:
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        scan.out.add("FS-SENSITIVE-FILE", "high", "A02", f"Ships a {full.name} credential directory",
                     f"{rel} should never be distributed with an app.", "Delete it from the app folder.", path=rel)
        return
    if stat.S_ISLNK(mode):
        _scan_link(scan, rel, full)
        return
    if not stat.S_ISREG(mode):
        return
    scan.total_bytes += info.st_size
    _file_hygiene(scan, rel, info)
    suffix = full.suffix.lower()
    if _is_native(full, suffix):
        scan.out.add("FS-NATIVE-CODE", "high", "A08", "Native executable or library",
                     "Compiled code cannot be reviewed and runs outside the browser sandbox.",
                     "Ship source (or WebAssembly) and build it in a declared runtime step.", path=rel)
        return
    if suffix not in TEXT_SUFFIXES and full.name not in {"Dockerfile", "Makefile", "Procfile"}:
        return
    if info.st_size > MAX_TEXT_FILE_BYTES or scan.text_bytes + info.st_size > MAX_TEXT_TOTAL_BYTES:
        scan.truncated = True
        scan.out.add("SCAN-SKIPPED", "low", "A09", "File too large to review",
                     "The review skipped this file; it may hide unreviewed code.",
                     "Split or minimize the file, or vendor a smaller build.", path=rel)
        return
    try:
        text = full.read_text("utf-8", errors="replace")
    except OSError:
        return
    scan.text_bytes += info.st_size
    scan.texts[rel] = text
    _scan_text(rel, text, suffix, scan.app_id, scan.out)


def _scan_text(rel: str, text: str, suffix: str, app_id: str, out: _Collector) -> None:
    minified = _is_minified(rel, text)
    lines = text.splitlines()
    for rule in RULES:
        if rule.suffixes and suffix not in rule.suffixes:
            continue
        if minified and not rule.minified_ok:
            continue
        for number, line in enumerate(lines, 1):
            if len(line) > 5000 and not rule.minified_ok:
                continue
            match = rule.pattern.search(line)
            if not match:
                continue
            if rule.unless_line is not None and rule.unless_line.search(line):
                continue
            if rule.code_only and _in_string(line, match.start()):
                continue
            secret_text = match.group(match.lastindex or 0)
            excerpt = _redact(secret_text) if rule.secret else _excerpt(line, match.start(), match.end())
            out.add(rule.rule, rule.severity, rule.owasp, rule.title, rule.detail, rule.remediation,
                    path=rel, line=number, excerpt=excerpt,
                    # A waiver covers exactly this statement: the whole line, not
                    # just the token, so a new sink added later is never pre-waived.
                    key=secret_text if rule.secret else line.strip())
    if suffix in JS or suffix in PY:
        if re.search(r"""addEventListener\s*\(\s*["']message["']|\bonmessage\s*=""", text) and not minified \
                and not re.search(r"\.origin\b|\.source\s*[!=]==?", text):
            out.add("ACL-MESSAGE-NO-ORIGIN", "medium", "A01", "message handler never checks the sender",
                    "A window 'message' listener without an origin/source check accepts commands from any page.",
                    "Compare event.origin (or event.source) against the expected sender first.", path=rel)
    for match in APP_STATE_REF.finditer(text):
        other = match.group(1)
        if other != app_id:
            number = text.count("\n", 0, match.start()) + 1
            out.add("ACL-CROSS-APP-STATE", "high", "A01", "Accesses another app's saved state",
                    f"References /api/app-state/{other} but this app is {app_id}.",
                    "Apps may only read and write their own state.", path=rel, line=number,
                    excerpt=match.group(0), key=other)
    if suffix in HTML:
        _external_tags(rel, text, out)
    _vulnerable_libraries(rel, text, out)


def _external_tags(rel: str, text: str, out: _Collector) -> None:
    for match in EXTERNAL_TAG.finditer(text):
        tag, before, _attr, url, after = match.groups()
        attrs = (before + after).lower()
        if tag.lower() == "link" and "stylesheet" not in attrs and "modulepreload" not in attrs:
            continue
        number = text.count("\n", 0, match.start()) + 1
        host = re.sub(r"^(?:https?:)?//", "", url).split("/")[0].lower()
        if url.startswith("http://") and not host.startswith(LOOPBACK_HOSTS):
            out.add("SUPPLY-CLEARTEXT", "high", "A08", "Remote code loaded over http://",
                    "A network attacker can replace this file in transit.", "Load it over https or vendor it.",
                    path=rel, line=number, excerpt=url)
        if tag.lower() == "script" and "integrity=" not in attrs:
            out.add("SUPPLY-NO-SRI", "medium", "A08", "Remote script without Subresource Integrity",
                    f"{url} can change after review and the browser will still run it.",
                    "Vendor the file into the app, or add integrity=\"sha384-…\" crossorigin=\"anonymous\".",
                    path=rel, line=number, excerpt=url)
        if any(host.endswith(cdn) for cdn in CDN_HOSTS):
            path = url.split(host, 1)[1]
            if re.search(r"@latest\b|@next\b", path) or (
                    host in {"cdn.jsdelivr.net", "unpkg.com", "esm.sh", "esm.run"}
                    and not re.search(r"@\d", path)) or host == "cdn.tailwindcss.com" and not re.search(r"/\d", path):
                out.add("SUPPLY-UNPINNED", "high", "A06", "Remote dependency is not pinned to a version",
                        f"{url} resolves to whatever version is newest, so reviewed code is not what runs.",
                        "Pin an exact version (pkg@1.2.3) and add SRI, or vendor it.",
                        path=rel, line=number, excerpt=url)


def _vulnerable_libraries(rel: str, text: str, out: _Collector) -> None:
    haystack = rel + "\n" + text[:4096]
    for name, pattern, fixed, severity, advisory in VULNERABLE_LIBRARIES:
        for match in pattern.finditer(haystack):
            found = next((g for g in match.groups() if g), None)
            version = _version_tuple(found) if found else None
            if version is None:
                continue
            limit = fixed
            if name == "bootstrap" and version[0] == 4:
                limit = _BOOTSTRAP4_FIXED
            if name == "bootstrap" and version[0] >= 5:
                continue
            if version < limit:
                out.add("VULN-LIBRARY", severity, "A06", f"Known-vulnerable {name} {found}",
                        advisory, f"Upgrade {name} to {'.'.join(map(str, limit))} or later.",
                        path=rel, excerpt=f"{name} {found}", key=f"{name}{found}")
            break


def _contract(root: Path, raw: dict, manifest, profile: str, out: _Collector) -> None:
    if profile == "store" and raw.get("id") not in (None, root.name) and root.name not in (".", "app"):
        out.add("CONTRACT-ID", "medium", "contract", "App id differs from its folder name",
                f"id {raw.get('id')!r} lives in folder {root.name!r}.", "Rename the folder to the app id.",
                path="app.json")
    if (root / "index.html").is_file():
        _contract_index(root, out)
    _contract_assets(root, manifest, out)
    _contract_browser_policy(raw, manifest, out)
    _contract_listing(raw, manifest, profile, out)


def _contract_index(root: Path, out: _Collector) -> None:
    try:
        index = (root / "index.html").read_text("utf-8", errors="replace")
    except OSError:
        index = ""
    if not re.search(r"(?i)<title>\s*\S", index):
        out.add("CONTRACT-TITLE", "low", "contract", "index.html has no <title>",
                "The browser tab and accessibility tools show a blank name.", "Add a <title>.",
                path="index.html")
    for match in LOCAL_REF.finditer(index):
        ref = match.group(1).strip()
        if ref and not re.match(r"(?i)^(?:[a-z][a-z0-9+.-]*:|//|data:|blob:)", ref):
            _contract_ref(root, ref, index.count("\n", 0, match.start()) + 1, out)


def _contract_ref(root: Path, ref: str, number: int, out: _Collector) -> None:
    """Classify one local asset reference from index.html."""
    where = {"path": "index.html", "line": number, "excerpt": ref}
    if ref.startswith(("/api/", "/apps/")):
        return
    if ref.startswith("/"):
        out.add("CONTRACT-ROOT-PATH", "medium", "contract", "Asset path is root-relative",
                f"{ref} resolves against the engine origin, not the app folder.",
                "Use a path relative to index.html.", **where)
        return
    if re.fullmatch(r"\.\./[^/]+\.[A-Za-z0-9]+", ref):
        out.add("CONTRACT-SHARED-FILE", "info", "contract", "Uses a shared host file",
                f"{ref} is served from the apps folder root (e.g. app-state-bridge.js); "
                "store-installed apps get it only when the local apps folder provides it.",
                "Vendor a copy into the app to be self-contained.", key=ref, **where)
        return
    target = (root / ref).resolve()
    if root not in target.parents and target != root:
        out.add("CONTRACT-ASSET-ESCAPE", "high", "contract", "Asset path leaves the app folder",
                f"{ref} points outside the app.", "Copy the asset into the app.", **where)
    elif not target.exists():
        out.add("CONTRACT-MISSING-ASSET", "medium", "contract", "index.html references a missing file",
                f"{ref} does not exist.", "Add the file or fix the path.", **where)


def _contract_assets(root: Path, manifest, out: _Collector) -> None:
    icon = manifest.icon
    if re.search(r"(?i)\.(?:png|svg|jpe?g|webp|gif|ico)$", icon) and "://" not in icon \
            and not (root / icon).is_file():
        out.add("CONTRACT-ICON", "high", "contract", "Icon file is missing",
                f"icon {icon!r} does not exist in the app.", "Add the icon or use an emoji.", path="app.json")
    meta = manifest.metadata
    for shot in meta.screenshots:
        if not (root / shot).is_file():
            out.add("CONTRACT-SCREENSHOT", "medium", "contract", "Screenshot file is missing",
                    f"{shot} does not exist.", path="app.json", key=shot)
    knowledge = meta.chat_knowledge
    if knowledge and not re.search(r"\s", knowledge) and "/" in knowledge and not (root / knowledge).exists():
        out.add("CONTRACT-KNOWLEDGE", "medium", "contract", "chat_knowledge path is missing",
                f"{knowledge} does not exist.", path="app.json")
    modules = (manifest.multiplayer.rules, manifest.multiplayer.ai) if manifest.multiplayer else ()
    for module in modules:
        if module and not (root / module).is_file():
            out.add("CONTRACT-MULTIPLAYER", "high", "contract", "Multiplayer module is missing",
                    f"{module} does not exist; the room service cannot load the game rules.",
                    path="app.json", key=module)


def _contract_browser_policy(raw: dict, manifest, out: _Collector) -> None:
    sandbox = manifest.browser.sandbox
    if raw.get("sandbox") is False or sandbox is False:
        out.add("CFG-NO-SANDBOX", "high", "A05", "Iframe sandbox disabled",
                "sandbox: false runs the app without browser sandbox restrictions.",
                "Remove `sandbox: false`; request specific sandbox tokens instead.", path="app.json")
    elif isinstance(sandbox, str):
        tokens = set(sandbox.split())
        for token, (severity, why) in SANDBOX_RISK.items():
            if token in tokens:
                out.add("CFG-SANDBOX-TOKEN", severity, "A05", f"Sandbox grants {token}",
                        f"{token} {why}.", "Drop the token unless the app cannot work without it.",
                        path="app.json", key=token)
    for permission in manifest.browser.permissions:
        if permission in {"camera", "microphone", "display-capture", "geolocation"}:
            out.add("PRIV-SENSITIVE-PERMISSION", "info", "A01", f"Requests {permission}",
                    "Disclosed to the user in the app's info dialog; the browser still prompts at use.",
                    path="app.json", key=permission)


def _contract_listing(raw: dict, manifest, profile: str, out: _Collector) -> None:
    meta = manifest.metadata
    if profile != "store":
        if not raw.get("version"):
            out.add("CONTRACT-VERSION", "low", "contract", "No version",
                    "Without a version the engine cannot order updates.", "Add \"version\": \"1.0.0\".",
                    path="app.json")
        return
    required = (("version", raw.get("version")), ("description", meta.description),
                ("author", meta.author), ("categories", meta.categories))
    for name in (name for name, value in required if not value):
        out.add("CONTRACT-LISTING", "high", "contract", f"Store listing needs `{name}`",
                f"Store releases must declare {name} so users can judge the app before installing.",
                f"Add `{name}` to app.json.", path="app.json", key=name, waivable=False)
    if meta.description and len(meta.description) < 20:
        out.add("CONTRACT-LISTING-SHORT", "low", "contract", "Description is very short",
                "Say what the app does in a sentence.", path="app.json")


def _permission_usage(manifest, texts: dict[str, str], out: _Collector) -> None:
    declared = set(manifest.browser.permissions)
    code = {rel: text for rel, text in texts.items()
            if Path(rel).suffix.lower() in CODE_SUFFIXES and not _is_minified(rel, text)}
    for permission, pattern in USES_PERMISSION.items():
        user = next((rel for rel, text in code.items() if pattern.search(text)), None)
        if user and permission not in declared and permission not in _UNDECLARED_OK:
            out.add("CONTRACT-PERMISSION-UNDECLARED", "medium", "contract",
                    f"Uses {permission} without declaring it",
                    f"{user} calls a {permission} API, but app.json `permissions` omits it, so the browser will block it.",
                    f"Add \"{permission}\" to permissions.", path=user, key=permission)
        if permission in declared and not user:
            out.add("PRIV-UNUSED-PERMISSION", "low", "A01", f"Declares {permission} but never uses it",
                    "Least privilege: request only capabilities the app uses.",
                    f"Remove \"{permission}\" from permissions.", path="app.json", key=permission)


def _dependencies(root: Path, texts: dict[str, str], out: _Collector) -> None:
    """Dependency manifests among the reviewed files (skipped dirs never reach here)."""
    for rel, text in texts.items():
        name = rel.rsplit("/", 1)[-1]
        if re.fullmatch(r"requirements[\w.-]*\.txt", name):
            _python_requirements(rel, text, out)
        elif name == "package.json":
            _npm_package(root, rel, text, out)


def _python_requirements(rel: str, text: str, out: _Collector) -> None:
    for number, line in enumerate(text.splitlines(), 1):
        spec = line.split("#", 1)[0].strip()
        if spec and not spec.startswith("-") and "==" not in spec and "@" not in spec:
            out.add("DEP-UNPINNED", "medium", "A06", "Python dependency is not pinned",
                    f"{spec} installs whatever version is newest at install time.",
                    "Pin with == (or ship a lock file).", path=rel, line=number, excerpt=spec, key=spec)


def _npm_package(root: Path, rel: str, text: str, out: _Collector) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    for hook in ("preinstall", "install", "postinstall"):
        if hook in scripts:
            out.add("DEP-INSTALL-SCRIPT", "medium", "A08", f"package.json runs a {hook} script",
                    f"{hook}: {str(scripts[hook])[:120]}", "Avoid install-time scripts.",
                    path=rel, key=hook)
    deps = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        if isinstance(data.get(section), dict):
            deps.update(data[section])
    for name, version in deps.items():
        version = str(version)
        if version in {"*", "latest", "", "x"} or version.startswith(("git", "http:", "github:")):
            out.add("DEP-UNPINNED", "medium", "A06", "npm dependency is not pinned",
                    f"{name}@{version} can change between installs.", "Pin a version range and commit a lock file.",
                    path=rel, excerpt=f"{name}@{version}", key=name)
    folder = (root / rel).parent
    if deps and not any((folder / lock).is_file()
                        for lock in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb")):
        out.add("DEP-NO-LOCKFILE", "low", "A06", "No lock file",
                "Without a lock file transitive dependencies are unpinned.", "Commit package-lock.json.",
                path=rel)


# ── Owner waivers and cached reports ─────────────────────────────────────────

class ReviewStore:
    """Cached reports and owner waivers under ``<state>/app-review``."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "app-review"
        self._lock = threading.RLock()
        self._memo: dict[tuple[str, str], tuple[float, str, ReviewReport]] = {}

    def _write(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(value, handle, indent=1)
            temp = Path(handle.name)
        temp.replace(path)

    def waivers(self, app_id: str) -> dict[str, str]:
        try:
            data = json.loads((self.dir / "waivers.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        entries = data.get(app_id, {}) if isinstance(data, dict) else {}
        return {str(k): str(v.get("reason", "")) for k, v in entries.items() if isinstance(v, dict)}

    def set_waiver(self, app_id: str, fingerprint: str, reason: str | None) -> None:
        with self._lock:
            path = self.dir / "waivers.json"
            try:
                data = json.loads(path.read_text("utf-8"))
                data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                data = {}
            entries = data.setdefault(app_id, {})
            if reason is None:
                entries.pop(fingerprint, None)
            else:
                entries[fingerprint] = {"reason": reason,
                                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            if not entries:
                data.pop(app_id, None)
            self._write(path, data)

    def _cache_path(self, root: Path, profile: str) -> Path:
        key = hashlib.sha256(f"{Path(root).resolve()}\0{profile}".encode()).hexdigest()[:24]
        return self.dir / "reports" / f"{key}.json"

    def report(self, root: Path, *, profile: str = "local", force: bool = False,
               max_age: float = 3.0) -> ReviewReport:
        """Return the current report, rescanning only when the app's files changed."""
        root = Path(root).resolve()
        memo_key = (str(root), profile)
        with self._lock:
            now = time.monotonic()
            cached = self._memo.get(memo_key)
            if cached and not force and now - cached[0] < max_age:
                report = cached[2]
            else:
                signature = tree_signature(root)
                report = None
                if not force:
                    if cached and cached[1] == signature:
                        report = cached[2]
                    else:
                        try:
                            stored = ReviewReport.from_dict(json.loads(self._cache_path(root, profile).read_text("utf-8")))
                            if stored.signature == signature and stored.review_version == REVIEW_VERSION:
                                report = stored
                        except (OSError, ValueError, TypeError, KeyError):
                            report = None
                if report is None:
                    report = review_app(root, profile=profile)
                    try:
                        self._write(self._cache_path(root, profile), report.to_dict())
                    except OSError:
                        pass
                self._memo[memo_key] = (now, report.signature, report)
        return report.with_waivers(self.waivers(report.app_id))


class ReviewGate:
    """Admission decision used by catalog discovery and store installation.

    ``mode`` comes from APP_ENGINE_APP_REVIEW: ``enforce`` (default) refuses
    apps with blocking findings; ``warn`` serves them but keeps the report;
    ``off`` skips review entirely.
    """

    def __init__(self, store: ReviewStore, mode: str = "enforce") -> None:
        mode = (mode or "enforce").strip().lower()
        if mode not in {"enforce", "warn", "off"}:
            raise ValueError("APP_ENGINE_APP_REVIEW must be enforce, warn or off")
        self.store = store
        self.mode = mode

    @classmethod
    def from_env(cls, state_dir: Path) -> "ReviewGate":
        return cls(ReviewStore(state_dir), os.environ.get("APP_ENGINE_APP_REVIEW", "enforce"))

    def admit(self, root: Path, *, profile: str = "local") -> tuple[bool, ReviewReport | None]:
        if self.mode == "off":
            return True, None
        report = self.store.report(root, profile=profile)
        return (report.passed or self.mode == "warn"), report


# ── CLI ──────────────────────────────────────────────────────────────────────

_COLOR = {"critical": "\033[1;31m", "high": "\033[31m", "medium": "\033[33m", "low": "\033[36m", "info": "\033[2m"}


def format_report(report: ReviewReport, *, color: bool = False) -> str:
    def paint(sev: str, text: str) -> str:
        return f"{_COLOR[sev]}{text}\033[0m" if color else text
    counts = report.counts()
    lines = [f"App Review — {report.label} ({report.app_id} {report.version or 'unversioned'}) · profile {report.profile}",
             f"Verdict: {'PASS' if report.passed else 'FAIL'} · score {report.score}/100 · "
             + " ".join(f"{name} {counts[name]}" for name in SEVERITIES) + f" · waived {counts['waived']}",
             f"Scanned {report.files_scanned} files, {report.bytes_scanned // 1024} KiB of text", ""]
    for item in report.findings:
        where = f"{item.path}:{item.line}" if item.line else item.path
        tag = " [waived]" if item.waived else (" [BLOCKING]" if item.blocking else "")
        lines.append(paint(item.severity, f"{item.severity.upper():8} {item.rule}{tag}") + f"  {item.title}")
        lines.append(f"         {item.category}" + (f" · {where}" if where else ""))
        lines.append(f"         {item.detail}")
        if item.excerpt:
            lines.append(f"         > {item.excerpt}")
        if item.remediation:
            lines.append(f"         fix: {item.remediation}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m app_engine.app_review",
                                     description="Review an app for app-engine admission (contract + OWASP security).")
    parser.add_argument("paths", nargs="+", type=Path, help="app folders (each containing app.json)")
    parser.add_argument("--profile", choices=PROFILES, default="local",
                        help="'store' also requires complete listing metadata and the package size limit")
    parser.add_argument("--json", action="store_true", help="print machine-readable reports")
    args = parser.parse_args(argv)
    failed = False
    reports = []
    for path in args.paths:
        report = review_app(path, profile=args.profile)
        failed |= not report.passed
        reports.append(report)
    if args.json:
        print(json.dumps([r.to_dict() for r in reports] if len(reports) > 1 else reports[0].to_dict(), indent=1))
    else:
        print("\n\n".join(format_report(r, color=sys.stdout.isatty()) for r in reports))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

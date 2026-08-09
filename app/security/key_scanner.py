"""Provider key security scanner.

Scans provider configurations for security risks:
1. Exfil URL - base_url points to suspicious destinations
2. Key injection - api_key contains shell/HTML/SQL injection patterns
3. URL spoofing - base_url impersonates known providers
4. Credential harvesting - base_url has credential-like query params
5. Local network exfil - base_url points to localhost/private IP for cloud APIs
6. Token tampering - OAuth tokens don't match expected format
7. Duplicate keys - same api_key across different providers
"""
from __future__ import annotations

import re
import ipaddress
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs

# Known provider domains (allowlist)
KNOWN_PROVIDER_DOMAINS = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "generativelanguage.googleapis.com": "gemini",
    "googleapis.com": "google",
    "openrouter.ai": "openrouter",
    "api.together.xyz": "together",
    "api.deepseek.com": "deepseek",
    "api.mistral.ai": "mistral",
    "api.cohere.ai": "cohere",
    "api.perplexity.ai": "perplexity",
    "api.siliconflow.com": "siliconflow",
    "api.groq.com": "groq",
    "api.fireworks.ai": "fireworks",
    "api.novita.ai": "novita",
    "api.moonshot.cn": "moonshot",
    "dashscope.aliyuncs.com": "qwen",
    "api.minimaxi.com": "minimaxi",
    "api.01.ai": "lingyi",
    "api.baichuan-ai.com": "baichuan",
    "api.stepfun.com": "stepfun",
    "open.bigmodel.cn": "zhipu",
}

# Suspicious URL patterns
SUSPICIOUS_DOMAINS = {
    "ngrok.io", "ngrok.app", "ngrok.dev",
    "pastebin.com", "paste.ee",
    "bit.ly", "tinyurl.com", "t.co",
    "webhook.site", "pipedream.net",
    "requestbin.com", "hookbin.com",
    "beeceptor.com", "mocky.io",
}

# Shell metacharacters in API keys (should never be there)
SHELL_INJECTION_PATTERNS = [
    re.compile(r'[;|`]'),
    re.compile(r'\$\('),
    re.compile(r'\$\{'),
    re.compile(r'\n|\r'),
    re.compile(r'<script', re.IGNORECASE),
    re.compile(r'<img[^>]+onerror', re.IGNORECASE),
    re.compile(r"'\s*OR\s+1=1", re.IGNORECASE),  # SQL injection
    re.compile(r"UNION\s+SELECT", re.IGNORECASE),
    re.compile(r'javascript:', re.IGNORECASE),
    re.compile(r'data:text/html', re.IGNORECASE),
]

# Credential harvesting query params
CREDENTIAL_PARAMS = {"key", "token", "api_key", "apikey", "secret", "password", "auth"}


@dataclass
class Finding:
    severity: str  # "block" | "warn" | "info"
    category: str
    provider: str
    connection: str
    message: str
    detail: str = ""


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    passed: bool = True
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "findings": [f.__dict__ for f in self.findings],
            "passed": self.passed,
            "summary": self.summary,
        }


def _is_private_ip(hostname: str) -> bool:
    """Check if hostname is a private/loopback IP."""
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return hostname in ("localhost", "0.0.0.0")


def _check_url_spoofing(base_url: str) -> Optional[str]:
    """Check if URL impersonates a known provider domain."""
    parsed = urlparse(base_url)
    host = parsed.hostname or ""

    for known_domain, provider_name in KNOWN_PROVIDER_DOMAINS.items():
        if known_domain in host and host != known_domain:
            # Known domain is a substring but not exact match
            # e.g. api.openai.com.evil.com
            if not host.endswith(known_domain):
                return provider_name

    if "xn--" in host:
        return "unknown (punycode)"

    return None


def scan_single_key(api_key: str, base_url: str, provider_format: str) -> list[Finding]:
    """Scan a single key/URL pair for security risks."""
    findings = []

    # 1. Key injection check
    if api_key:
        for pattern in SHELL_INJECTION_PATTERNS:
            if pattern.search(api_key):
                findings.append(Finding(
                    severity="block",
                    category="key_injection",
                    provider="",
                    connection="",
                    message=f"API key contains suspicious pattern: {pattern.pattern}",
                    detail="Key matches injection pattern. This could indicate a crafted key.",
                ))
                break

    if not base_url:
        return findings

    parsed = urlparse(base_url)
    host = parsed.hostname or ""

    # 2. Exfil URL check (match domain or any subdomain)
    for sus_domain in SUSPICIOUS_DOMAINS:
        if host == sus_domain or host.endswith("." + sus_domain):
            findings.append(Finding(
                severity="block",
                category="exfil_url",
                provider="",
                connection="",
                message=f"base_url points to suspicious domain: {host}",
                detail=f"This domain ({host}) is commonly used for data exfiltration.",
            ))
            break

    # 3. Credential harvesting check
    query = parsed.query
    if query:
        params = parse_qs(query)
        for param_name in params:
            if param_name.lower() in CREDENTIAL_PARAMS:
                findings.append(Finding(
                    severity="block",
                    category="credential_harvesting",
                    provider="",
                    connection="",
                    message=f"base_url contains credential-like query parameter: '{param_name}'",
                    detail=f"Query parameter '{param_name}' may be used to harvest credentials.",
                ))

    # 4. Local network exfil check (only for cloud API formats)
    cloud_formats = {"openai", "openai-responses", "anthropic", "gemini",
                     "openai-image", "openai-video"}
    if provider_format in cloud_formats:
        if _is_private_ip(host):
            findings.append(Finding(
                severity="block",
                category="local_network_exfil",
                provider="",
                connection="",
                message=f"base_url points to local/private network: {host}",
                detail=f"Provider format '{provider_format}' implies a cloud API, but base_url points to {host}.",
            ))

    # 5. HTTP (not HTTPS) check
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1"):
        findings.append(Finding(
            severity="block",
            category="insecure_transport",
            provider="",
            connection="",
            message=f"base_url uses insecure HTTP: {base_url}",
            detail="HTTP transmits API keys in cleartext. Use HTTPS.",
        ))

    # 6. URL spoofing check
    spoofed = _check_url_spoofing(base_url)
    if spoofed:
        findings.append(Finding(
            severity="warn",
            category="url_spoofing",
            provider="",
            connection="",
            message=f"base_url may impersonate {spoofed}",
            detail=f"Domain '{host}' contains a known provider name but is not the official domain.",
        ))

    # 7. Raw IP check (suspicious for cloud APIs)
    if provider_format in cloud_formats:
        try:
            ipaddress.ip_address(host)
            findings.append(Finding(
                severity="warn",
                category="raw_ip_url",
                provider="",
                connection="",
                message=f"base_url uses raw IP address: {host}",
                detail="Cloud APIs should use domain names, not raw IPs.",
            ))
        except ValueError:
            pass

    return findings


def scan_provider_config(config: dict) -> ScanResult:
    """Scan all provider configurations for security risks."""
    result = ScanResult()
    providers = config.get("providers", {})

    key_registry: dict[str, list[tuple[str, str]]] = {}

    if not isinstance(providers, dict):
        result.summary = "No providers configured."
        result.passed = True
        return result

    for prov_id, prov_data in providers.items():
        if not isinstance(prov_data, dict):
            continue

        provider_format = prov_data.get("format", "openai")
        connections = prov_data.get("connections", [])

        if not isinstance(connections, list):
            connections = []

        for i, conn in enumerate(connections):
            if not isinstance(conn, dict):
                continue

            conn_name = conn.get("name", f"connection-{i}")
            api_key = conn.get("api_key", "")
            base_url = conn.get("base_url", "")
            token_type = conn.get("token_type", "")
            refresh_token = conn.get("refresh_token", "")

            findings = scan_single_key(api_key, base_url, provider_format)
            for f in findings:
                f.provider = prov_id
                f.connection = conn_name
                result.findings.append(f)

            # Token tampering check
            if token_type == "oauth" and refresh_token:
                prov_lower = prov_id.lower()
                if "google" in prov_lower or "gemini" in prov_lower:
                    if not refresh_token.startswith("1//"):
                        result.findings.append(Finding(
                            severity="warn",
                            category="token_tampering",
                            provider=prov_id,
                            connection=conn_name,
                            message="OAuth refresh_token doesn't match expected Google format",
                            detail="Google refresh tokens typically start with '1//'.",
                        ))

            if api_key:
                key_registry.setdefault(api_key, []).append((prov_id, conn_name))

    # Duplicate keys check
    for key, locations in key_registry.items():
        if len(locations) > 1:
            providers_list = ", ".join([f"{p}/{c}" for p, c in locations])
            result.findings.append(Finding(
                severity="warn",
                category="duplicate_keys",
                provider=locations[0][0],
                connection=locations[0][1],
                message=f"Same API key used in {len(locations)} providers",
                detail=f"Key appears in: {providers_list}.",
            ))

    result.passed = not any(f.severity == "block" for f in result.findings)

    blocks = sum(1 for f in result.findings if f.severity == "block")
    warns = sum(1 for f in result.findings if f.severity == "warn")
    if blocks:
        result.summary = f"Scan FAILED: {blocks} blocking issue(s), {warns} warning(s)."
    elif warns:
        result.summary = f"Scan PASSED with warnings: {warns} warning(s) found."
    else:
        result.summary = "Scan PASSED: no issues found."

    return result

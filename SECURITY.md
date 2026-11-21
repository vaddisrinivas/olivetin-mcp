# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | Current development line |
| 0.1.x   | Upgrade recommended |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public GitHub issue
2. Email **vaddisrinivas170497@gmail.com** with:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
3. You will receive an acknowledgment within **48 hours**
4. A fix will be developed and released as soon as possible (target: within 5 business days for critical issues)

## Security Design

This project is designed with security as a priority:

- **Non-root container**: Runs as `bridge:10001` with no login shell
- **Stripped attack surface**: All shells, network tools, and compilers removed from the container
- **No runtime package installation**: pip is uninstalled after build
- **Read-only filesystem**: Designed to run with `read_only: true` in Docker Compose
- **Dropped capabilities**: All Linux capabilities can be dropped
- **Human-in-the-loop**: Destructive actions require explicit human approval
- **XSS protection**: Jinja2 templates use `autoescape=True`
- **Cryptographic tokens**: Approval tokens use `secrets.token_urlsafe(16)`
- **Security headers**: All HTML responses include `X-Frame-Options: DENY`, `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`
- **SSRF protection**: External-facing tools check for disallowed URI schemes (`file://`, `javascript:`, `data:`, `gopher://`, etc.)
- **Error sanitization**: Error responses do not leak internal server details or stack traces

## Threat Model

### Threats and Mitigations

| Threat | Severity | Mitigation |
|--------|----------|-----------|
| Compromised MCP client auto-approving actions | High | Elicitation trusts the client. URL approvals trust whoever can access their token and endpoint. Use separately authenticated human ingress and prevent agent access to approval POST routes when independent approval is required. |
| Container escape | High | Non-root user, no shells/tools, read-only filesystem, all capabilities dropped. |
| Sensitive diagram/chart content sent externally | Medium | Rendering uses mermaid.ink and quickchart.io. Avoid sending private content. Diagram scheme filtering is a partial content check, not a renderer sandbox. |
| XSS in approval UI | Medium | Jinja2 `autoescape=True`; CSP `default-src 'none'`; `X-Frame-Options: DENY`. |
| Approval token brute-force | Low | Tokens use 128 bits of entropy (`secrets.token_urlsafe(16)`); tokens expire after `APPROVAL_TIMEOUT_SECS`. |
| Credential leakage in logs | Low | Error messages are sanitized; OliveTin response bodies are not included in user-facing errors. |
| Clickjacking on approval UI | Low | `X-Frame-Options: DENY` prevents embedding in iframes. |

### Deployment boundaries

Network transports bind to `127.0.0.1` by default; the Docker image listens on all container interfaces, with Compose publishing loopback ports only. The bridge has no built-in inbound authentication. OliveTin credentials authenticate the bridge to OliveTin, not callers to the bridge. Protect remote MCP and approval routes with authenticated TLS ingress before exposing them.

An accepted elicitation executes immediately; URL approval is a fallback, not a mandatory second factor. An MCP client with HTTP access to a disclosed approval URL can approve it. Cryptographic URLs prevent guessing; they do not establish human identity. Stdio has no HTTP approval listener: configure a custom approval backend or use a client with elicitation support.

Modern MCP continuations carry framework-sealed request state. The bridge also records a bounded set of expiring, single-use nonces bound to each requested tool and argument object. A replay or altered continuation fails closed. These approvals and URL approvals are process local; use a single bridge process. A restart invalidates pending approvals.

Read-only action prefixes are operator assertions, not proof of command safety. Anyone allowed to edit action configuration can change that classification. Reserved bridge tool names cannot be replaced by dashboard actions.

Only known read RPCs retry network errors. Action starts, restarts, kills, and unknown RPCs run once per request. After a lost write response, check logs before any manually authorized retry. Logs and action arguments may contain sensitive data; redact them before sharing.

## Dependency Policy

- All dependencies must use OSI-approved licenses compatible with MIT
- Dependencies are monitored via Dependabot for security advisories
- The `uv.lock` file pins exact versions for reproducible builds

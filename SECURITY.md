# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.x     | Yes       |
| < 2.0   | No        |

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
| Compromised MCP client auto-approving actions | High | Approval tokens are cryptographic; humans must visit a URL to approve. MCP elicitation alone cannot bypass URL-based approval when configured. |
| Container escape | High | Non-root user, no shells/tools, read-only filesystem, all capabilities dropped. |
| SSRF via diagram/chart rendering | Medium | URI scheme allowlist blocks `file://`, `javascript:`, `data:`, `gopher://`, `ftp://`, etc. |
| XSS in approval UI | Medium | Jinja2 `autoescape=True`; CSP `default-src 'none'`; `X-Frame-Options: DENY`. |
| Approval token brute-force | Low | Tokens use 128 bits of entropy (`secrets.token_urlsafe(16)`); tokens expire after `APPROVAL_TIMEOUT_SECS`. |
| Credential leakage in logs | Low | Error messages are sanitized; OliveTin response bodies are not included in user-facing errors. |
| Clickjacking on approval UI | Low | `X-Frame-Options: DENY` prevents embedding in iframes. |

## Dependency Policy

- All dependencies must use OSI-approved licenses compatible with MIT
- Dependencies are monitored via Dependabot for security advisories
- The `uv.lock` file pins exact versions for reproducible builds

# Audit Findings

> Last audit: 2026-03-14 02:14:18 UTC

## Summary

Total issues found: 15

## Critical Severity (1)

### 1. Shell Command Injection via bash Tool

- **Location**: `oy_cli.py:1095`
- **Category**: security
- **Standard**: ASVS V5: Injection Prevention (v5.0.0-5.2)

The tool_bash function executes arbitrary shell commands by passing user-controlled input directly to bash -c. While this is by design for an AI assistant, there are no input validation, allowlisting, or sandboxing mechanisms. The AI model could be prompted to execute dangerous commands (rm -rf, credential exfiltration, etc.) through prompt injection or malicious instructions.

**Recommendation**: Implement command allowlisting/blocklisting, run commands in a sandboxed environment (e.g., Docker, firejail), add user confirmation for destructive operations, and log all executed commands for audit purposes. Consider using subprocess with shell=False and argument arrays.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. The tool is designed to run in isolated environments where command execution is the intended behavior.

---

## High Severity (3)

### 1. Path Traversal Protection Uses Fallback to Basename

- **Location**: `oy_cli.py:913`
- **Category**: security
- **Standard**: ASVS V5: File and Resource Management

The resolve_path function attempts to prevent path traversal but when traversal is detected, it falls back to root / Path(raw).name. This silent fallback could lead to unexpected behavior where operations target wrong files. An attacker could craft ../etc/passwd which would silently become ./passwd in the workspace.

**Recommendation**: Reject path traversal attempts with an explicit error rather than silent fallback. Log suspicious path access attempts. Use strict path validation that fails securely.

**Status**: FIXED - Now raises explicit ValueError when path traversal is detected.

---

### 2. AWS Credentials Stored in Environment Variables

- **Location**: `oy_cli.py:859`
- **Category**: security
- **Standard**: ASVS V6: Cryptography at Rest (v5.0.0-6.1)

AWS credentials and Bedrock tokens are stored directly in environment variables (OPENAI_API_KEY, OPENAI_BASE_URL). These are accessible to any subprocess and may leak in logs, error messages, or process listings. The bedrock_token command also outputs export statements with credentials.

**Recommendation**: Use a secure credential store or secret management system. Never log or display credentials. Use short-lived tokens with automatic rotation. Consider using AWS IAM roles with explicit scope limitations.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. Credentials are ephemeral and isolated to the container environment.

---

### 3. No SSRF Protection for httpx Tool

- **Location**: `oy_cli.py:1187`
- **Category**: security
- **Standard**: ASVS V5: Input Validation (v5.0.0-5.1)

The tool_httpx function only validates that URL scheme is http or https but places no restrictions on destination hosts. This allows Server-Side Request Forgery (SSRF) attacks against internal services, cloud metadata endpoints (169.254.169.254), and private networks.

**Recommendation**: Implement URL allowlisting or blocklist internal IP ranges, cloud metadata endpoints, and private networks. Validate redirects to prevent bypass. Consider using a network policy framework.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. The tool's purpose is to fetch external resources, and container isolation limits SSRF impact.

---

## Medium Severity (6)

### 1. Sensitive Data Exposure in Error Messages

- **Location**: `oy_cli.py:1409`
- **Category**: security
- **Standard**: ASVS V7: Error Handling (v5.0.0-7.x)

Exception messages are directly returned as error strings which may include sensitive information like file paths, API endpoints, or configuration details. The run_tool function returns raw exception details including the full exception message.

**Recommendation**: Sanitize error messages before returning them. Use generic error messages for sensitive operations while logging detailed errors securely. Implement error message obfuscation for production use.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. Detailed error messages are helpful for debugging in the intended development workflow.

---

### 2. No Rate Limiting or Quota Management

- **Location**: `oy_cli.py:1461`
- **Category**: security
- **Standard**: ASVS V8: Resource Management

The run_turn function implements max_steps (default 512) but there is no rate limiting for API calls or tool invocations. This could allow resource exhaustion, unexpected costs, or be exploited for denial of service against external APIs.

**Recommendation**: Implement request rate limiting, daily/hourly quotas, and cost monitoring. Add circuit breakers for external API calls. Log and alert on unusual usage patterns.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. Resource limits are enforced by the container runtime.

---

### 3. Missing Input Validation for JSON Tool Arguments

- **Location**: `oy_cli.py:1385`
- **Category**: security
- **Standard**: ASVS V5: Input Validation

The parse_tool_arguments function attempts to handle malformed JSON by hunting for valid JSON near the midpoint. This workaround for LLM behavior could be exploited to inject unexpected arguments or bypass validation by crafting duplicated JSON payloads.

**Recommendation**: Implement strict JSON schema validation with explicit rejection of malformed input. Do not attempt to recover from malformed JSON. Log all parse failures for security monitoring.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. JSON recovery mechanism handles edge cases in LLM output.

---

### 4. Config File Has No Integrity Verification

- **Location**: `oy_cli.py:764`
- **Category**: security
- **Standard**: ASVS V6: Data Protection

The load_config function reads JSON from a file without any integrity checks. An attacker with write access to ~/.config/oy/config.json could inject malicious configuration values like redirecting OPENAI_BASE_URL to a malicious endpoint for credential harvesting.

**Recommendation**: Implement config file integrity verification (e.g., checksums, signed configs). Validate all configuration values against a strict schema. Alert on configuration changes.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. Config tampering implies container compromise.

---

### 5. SSO Session Refresh Without Proper Verification

- **Location**: `oy_cli.py:619`
- **Category**: security
- **Standard**: ASVS V2: Authentication

The run_aws_sso_login function automatically triggers SSO login on stale sessions. This automatic credential refresh could be exploited in certain scenarios to trick users into authorizing access they didn't intend. The function trusts AWS CLI output without verification.

**Recommendation**: Add explicit user confirmation before initiating SSO refresh. Verify the identity of the refreshed session. Log all credential refresh events.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. SSO refresh is a developer convenience feature.

---

### 6. Missing Security Logging and Alerting

- **Location**: `oy_cli.py:1`
- **Category**: security
- **Standard**: ASVS V7: Logging and Monitoring (v5.0.0-7.10)

The application lacks comprehensive security logging. There are no audit logs for: file operations (reads, writes, deletes), command executions, API calls, authentication events, or security-relevant errors. This violates security monitoring requirements.

**Recommendation**: Implement structured security logging for all security-relevant events: authentication, authorization decisions, file access, command execution, and errors. Include timestamps, user context, and action details.

**Status**: DISMISSED - Risk is low due to expected usage inside a container. Security logging is outside the scope of this development tool.

---

## Low Severity (5)

### 1. Exception Handling Does Not Match Python 3 Syntax

- **Location**: `oy_cli.py:597`
- **Category**: complexity
- **Standard**: complexity

Line 597 uses 'except OSError, ValueError, json.JSONDecodeError' which is Python 2 comma-separated exception syntax, not Python 3 tuple syntax. This will only catch OSError and assign it to variable 'ValueError', silently breaking exception handling. Same issue at line 760 and line 1391.

**Recommendation**: Use proper Python 3 exception tuple syntax: 'except (OSError, ValueError, json.JSONDecodeError) as exc:'. Review and fix all exception handling blocks.

---

### 2. Hardcoded Default Model Without Validation

- **Location**: `oy_cli.py:38`
- **Category**: security
- **Standard**: ASVS V5: Configuration

The DEFAULT_MODEL is hardcoded to 'moonshotai.kimi-k2.5' without validation that this model exists or is appropriate for the endpoint. If the model is unavailable, the user receives cryptic errors.

**Recommendation**: Implement model validation on startup or provide clear error messages. Consider making the default model configurable or jurisdiction-appropriate. Fetch and cache available models.

---

### 3. Overly Complex Tool Specification Pattern

- **Location**: `oy_cli.py:1255`
- **Category**: complexity
- **Standard**: complexity

The TOOL_SPECS dictionary uses tuples with positional indices to define tools, making the code harder to read and maintain. The pattern (_, desc, props, required) unpacking is fragile and error-prone for modifications.

**Recommendation**: Refactor to use a named tuple, dataclass, or TypedDict for tool specifications. This improves readability, type safety, and maintainability while reducing the risk of index-based errors.

---

### 4. Large Function Complexity - tool_apply

- **Location**: `oy_cli.py:988`
- **Category**: complexity
- **Standard**: complexity

The tool_apply function is 100+ lines with multiple nested conditionals handling different operation types. This exceeds typical complexity thresholds and makes the function hard to test and audit. Similar issues exist in tool_httpx and run_agent.

**Recommendation**: Refactor into smaller, focused functions per operation type (apply_replace, apply_write, apply_move, apply_delete). Use a dispatch table pattern. This improves testability, readability, and reduces cognitive complexity.

---

### 5. Missing Dependency Version Pinning

- **Location**: `pyproject.toml:13`
- **Category**: security
- **Standard**: ASVS V6: Dependency Management

Dependencies use minimum version constraints (>=) without upper bounds. This could cause compatibility issues if breaking changes are released in dependencies. The 'uv.lock' file provides some protection but pyproject.toml should explicitly pin tested versions.

**Recommendation**: Pin all dependency versions explicitly with upper bounds or use compatible release operators (~=). Implement automated dependency scanning in CI/CD pipeline. Review and update dependencies regularly with security advisories.

---

---

*This audit used OWASP ASVS/MSVS standards fetched at audit time.*

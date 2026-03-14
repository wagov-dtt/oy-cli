# Audit Findings

> Last audit: 2025-06-17 (OWASP ASVS 5.0.0 / MSVS)

## Summary

Total issues found: 6

## Critical Severity (1)

### 1. Python 2 Exception Handling Syntax Breaks Error Handling

- **Location**: `oy_cli.py:601, 764, 1397`
- **Category**: security, bug
- **Standard**: ASVS V5: Input Validation and Error Handling

Three locations use Python 2 comma-separated exception syntax instead of Python 3 parenthesized tuples:

1. Line 601: `except OSError, ValueError, json.JSONDecodeError:`
2. Line 764: `except OSError, json.JSONDecodeError:`
3. Line 1397: `except json.JSONDecodeError, ValueError:`

This syntax is valid Python 2 but incorrect in Python 3. In Python 3, this syntax catches only the first exception type and assigns it to a variable named after the second type. For example, `except OSError, json.JSONDecodeError` catches only OSError and assigns it to a variable named `json.JSONDecodeError`, which silently breaks the intended multi-exception handling.

This could lead to:
- Uncaught exceptions in error paths
- Misleading variable names masking real exceptions
- Silent failures when loading config files or parsing JSON
- Potential security bypasses if error handling is security-relevant

**Recommendation**: Fix all three occurrences:

```python
# Line 601
except (OSError, ValueError, json.JSONDecodeError):

# Line 764  
except (OSError, json.JSONDecodeError):

# Line 1397
except (json.JSONDecodeError, ValueError):
```

**Status**: FIXED - 2025-06-17 (commit pending)

---

## High Severity (1)

### 1. Missing Unit Tests and Test Coverage

- **Location**: Project root
- **Category**: security, quality
- **Standard**: ASVS V8: Secure Software Lifecycle

The project has no unit tests or test suite. This is critical for a security-sensitive tool that:
- Executes arbitrary shell commands
- Modifies user files
- Handles authentication credentials
- Parses untrusted input from LLMs

Without tests, there's no way to verify:
- Path traversal protections work correctly
- Input validation is comprehensive
- Error handling behaves as expected
- Security patches don't introduce regressions

**Recommendation**: 
1. Add a test suite using pytest
2. Achieve minimum 80% code coverage
3. Include specific security tests for:
   - Path traversal prevention (resolve_path)
   - Command injection scenarios
   - Input validation edge cases
   - Error handling paths
4. Add tests to CI/CD pipeline (see missing CI configuration below)

**Status**: OPEN - Critical for security assurance

---

## Medium Severity (2)

### 1. Missing CI/CD Pipeline for Automated Security Checks

- **Location**: `.github/` directory
- **Category**: security, quality
- **Standard**: ASVS V8: Secure Deployment

The `.github` directory exists but appears to be empty or minimal. This project lacks:
- Automated testing on pull requests
- Automated linting and formatting checks
- Automated security scanning
- Build verification before merge
- Dependency vulnerability scanning

**Recommendation**:
1. Add GitHub Actions workflow for:
   - Running ruff lint and format checks
   - Running tests (once added)
   - Building the package
   - Scanning for dependency vulnerabilities (e.g., pip-audit, dependabot)
2. Require all checks to pass before merging PRs
3. Add branch protection rules

**Status**: OPEN - Recommended for secure development lifecycle

---

### 2. Missing Pre-commit Hooks

- **Location**: Project configuration
- **Category**: security, quality
- **Standard**: ASVS V8: Secure Development Practices

No pre-commit hooks are configured to catch common issues before commit:
- Syntax errors
- Formatting violations
- Basic linting issues
- Secret/key detection

**Recommendation**:
1. Add `.pre-commit-config.yaml` with hooks for:
   - ruff (linting and formatting)
   - check-yaml
   - check-json
   - detect-secrets or gitleaks
2. Document setup in CONTRIBUTING.md

**Status**: OPEN - Recommended for development hygiene

---

## Low Severity (2)

### 1. Hardcoded Default Model Without Validation

- **Location**: `oy_cli.py:38`
- **Category**: security, usability
- **Standard**: ASVS V5: Configuration

The `DEFAULT_MODEL` is hardcoded to `'moonshotai.kimi-k2.5'` without validation that this model exists or is appropriate for the endpoint. If the model is unavailable, users receive cryptic errors.

**Recommendation**: 
1. Validate model availability on startup or first use
2. Provide clear error messages when default model is unavailable
3. Consider making default model configurable per installation

**Status**: OPEN - Minor usability improvement

---

### 2. Hardcoded Timeouts and Limits Could Cause Production Issues

- **Location**: `oy_cli.py:35-43, 562, 1097, 1200`
- **Category**: complexity, operations
- **Standard**: ASVS V8: Resource Management

Several operational limits are hardcoded as constants:
- MAX_TOOL_OUTPUT_CHARS = 16000
- DEFAULT_MAX_STEPS = 512
- DEFAULT_MAX_TOOL_CALLS = 512
- Default bash timeout = 120 seconds
- Default httpx timeout = 20 seconds

These cannot be tuned for different environments or use cases without code changes.

**Recommendation**:
1. Make these configurable via environment variables
2. Document recommended values for different scenarios
3. Add config file support for operational parameters

**Status**: OPEN - Minor operational improvement

---

## Security Strengths

The codebase demonstrates several good security practices:

1. **Path Traversal Protection**: Line 910-919 implements proper path resolution with explicit ValueError on traversal attempts (fixed from prior audit)

2. **Header Redaction**: Line 416-422 properly redacts sensitive headers (Authorization, Cookie, etc.) in httpx output

3. **No Dangerous Patterns**: No use of eval(), exec(), pickle, marshal, or other high-risk patterns

4. **Subprocess Safety**: Uses `subprocess.run()` with explicit argument lists, not `shell=True` (except for bash tool which is the intended design)

5. **Error Recovery**: Non-interactive mode includes documented error recovery guidance for resilience

6. **Credential Handling**: AWS credentials are obtained via official AWS CLI, not stored in files

7. **Small Attack Surface**: ~1820 lines of straightforward code is auditable

---

## Recommendations Summary

**Immediate Actions (Critical)**:
1. Fix Python 2 exception syntax bug (3 locations)

**High Priority**:
1. Add comprehensive unit test suite
2. Set up CI/CD pipeline with automated checks

**Medium Priority**:
1. Add pre-commit hooks
2. Add security-focused tests

**Low Priority**:
1. Make operational parameters configurable
2. Improve default model validation

---

## Notes from Previous Audits

The following issues from the previous audit (2026-03-14) were reviewed and their DISMISSED statuses are confirmed appropriate for this tool's design goals:

- Shell command injection via bash tool: Acceptable risk given container-based usage model
- SSRF protection: Acceptable given tool's purpose to fetch external resources
- Credential handling: Ephemeral credentials in environment is acceptable for CLI tool
- Logging: Security logging outside scope for development tool

The path traversal issue from the previous audit has been **FIXED** and is now handled correctly with an explicit ValueError.

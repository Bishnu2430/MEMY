---
name: owasp-checklist
description: OWASP-oriented security checks for web and data code (injection, secrets, deserialization, SSRF).
---
# Security checklist
- Injection: SQL built with string formatting; shell=True with user input; eval/exec.
- Secrets: hard-coded keys, tokens or passwords; secrets in logs.
- Deserialization: pickle/yaml.load on untrusted data (use yaml.safe_load).
- SSRF / path traversal: user-controlled URLs or file paths without allowlists.
- Crypto: md5/sha1 for passwords; missing TLS verification (verify=False).
Severity high = exploitable with untrusted input.

# Rule sets (YAML, versioned)

Compliance standards as data — one YAML file per standard, loaded at run time
(architecture §7.3). The two ratified first standards (§13 Q1) are authored as
**scoped subsets** in a later Phase 0 sprint:

- `iso-27001.yaml` — scoped subset first: access control + logging annexes
  (~20–40 rules).
- `pci-dss.yaml` — scoped subset first: Requirement 10 log-management
  (~20–40 rules).

The global central-bank/institution ambition is direction, not MVP scope (§13 Q1).

Rule-set schema (from §7.3):

```yaml
standard: ISO-27001
version: 1
rules:
  - id: ISO-001
    category: access_control
    severity: high        # high|medium|low|info
    check: presence       # presence|threshold|format|reference|judgment
    target: access_logs.authentication_failure
    params: { required: true }
    llm_assist: false
```

Findings always cite `rule_id + standard + version`, so every report line is
traceable to the exact rule that produced it.

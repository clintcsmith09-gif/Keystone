"""Per-rule-set ingestion profiles (Block B).

A profile declares, in the same versioned YAML style as the rule sets, which
fields an audit expects and which source-column aliases mean those fields. It is
what lets an ISO 27001 audit read ``principal`` or ``User ID`` as ``user_id``
without changing the rule set or the corpus.

Where it lives: the ``ingest:`` block of each rule-set file
(``rules/iso27001.yaml``, ``rules/pci-dss.yaml``), loaded by
``app.audit.rules.load_ruleset``. See ``veritas/docs/ingestion.md`` for the
schema and the supported aliases.

Design rules (kept deliberately conservative):
  * A source column that already equals a canonical field name is never renamed.
  * An alias only renames when the canonical field is not already present;
    otherwise the alias is recorded as an ``ambiguous_alias`` issue and the
    original column is kept.
  * Applying a profile never drops a column or a row — it only renames, and it
    records what it did in the normalize artifact (``profile`` key).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..uploads.errors import make_error

MAX_ROW_ERRORS = 50
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _key(name: str) -> str:
    """Case/punctuation-insensitive key: 'User ID' → 'user_id', 'userId' → 'userid'."""
    return _NON_ALNUM.sub("_", str(name).strip().lower()).strip("_")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    type: str = "string"
    description: str = ""


@dataclass(frozen=True)
class IngestProfile:
    standard: str
    version: int
    fields: tuple[FieldSpec, ...]
    options: dict = field(default_factory=dict)

    @property
    def canonical(self) -> set[str]:
        return {f.name for f in self.fields}

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    def alias_map(self) -> dict[str, str]:
        """Normalized alias → canonical field name (canonical names included)."""
        mapping: dict[str, str] = {}
        for spec in self.fields:
            mapping[_key(spec.name)] = spec.name
        for spec in self.fields:
            for alias in spec.aliases:
                mapping.setdefault(_key(alias), spec.name)
        return mapping

    def as_dict(self) -> dict:
        return {
            "standard": self.standard,
            "version": self.version,
            "fields": [
                {
                    "name": f.name,
                    "aliases": list(f.aliases),
                    "required": f.required,
                    "type": f.type,
                    "description": f.description,
                }
                for f in self.fields
            ],
            "options": dict(self.options),
        }


def profile_from_dict(standard: str, raw: dict | None, *,
                      version: int = 1) -> IngestProfile | None:
    """Build an IngestProfile from a rule set's ``ingest:`` block.

    Returns None when the block is absent, so callers can treat profiles as
    optional and keep the pre-Block-B behaviour.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{standard}: 'ingest' must be a mapping")
    profile_version = int(raw.get("version", version))
    fields: list[FieldSpec] = []
    raw_fields = raw.get("fields") or {}
    if isinstance(raw_fields, list):  # tolerate a list-of-mappings form
        raw_fields = {
            str(item["name"]): {k: v for k, v in item.items() if k != "name"}
            for item in raw_fields
            if isinstance(item, dict) and item.get("name")
        }
    for name, spec in raw_fields.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(f"{standard}: ingest field {name!r} must be a mapping")
        aliases = spec.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        fields.append(FieldSpec(
            name=str(name),
            aliases=tuple(str(a) for a in aliases),
            required=bool(spec.get("required", False)),
            type=str(spec.get("type", "string")),
            description=str(spec.get("description", "")),
        ))
    if not fields:
        raise ValueError(f"{standard}: 'ingest.fields' is empty")
    options: dict = {}
    for block in ("csv", "xlsx"):
        if isinstance(raw.get(block), dict):
            options.update({k: v for k, v in raw[block].items() if v is not None})
    for key in ("header_case_insensitive", "trim_values", "sheet"):
        if key in raw and raw[key] is not None:
            options[key] = raw[key]
    return IngestProfile(
        standard=standard, version=profile_version,
        fields=tuple(fields), options=options,
    )


def load_profile(standard: str, rules_dir=None) -> IngestProfile | None:
    """Load the ingest profile attached to a rule set (None if it has none)."""
    from .rules import get_ruleset

    rule_set = get_ruleset(standard, rules_dir)
    if rule_set is None:
        return None
    return profile_from_dict(rule_set.standard, getattr(rule_set, "ingest", None),
                             version=rule_set.version)


# --- applying a profile -------------------------------------------------------
def apply_profile(view: dict, profile: IngestProfile, *,
                  file: str | None = None) -> tuple[dict, dict]:
    """Rename aliased columns in ``view`` in place-safe fashion.

    Returns ``(view, report)``. ``report`` is JSON-serialisable and is stored on
    the normalize artifact under ``profile``: matched fields, renamed columns,
    unmatched source columns, missing required fields and issues.
    """
    columns = list(view.get("columns") or [])
    alias_map = profile.alias_map()
    canonical = profile.canonical
    present = set(columns)
    issues: list[dict] = []

    rename: dict[str, str] = {}
    matched: dict[str, str] = {}
    for col in columns:
        if col in canonical:
            matched[col] = col
            continue
        target = alias_map.get(_key(col))
        if target is None:
            continue
        if target in present and target != col:
            issues.append(make_error(
                "ambiguous_alias",
                f"Column '{col}' looks like '{target}', but the file already has "
                f"a '{target}' column; the original column was kept.",
                file=file, row=None, column=col, value=target,
                rule=f"{profile.standard}.ingest.aliases",
                hint="Rename one of the two columns in the export.",
            ))
            continue
        rename[col] = target
        matched[target] = col

    if rename:
        columns = [rename.get(c, c) for c in columns]
        new_rows = []
        for row in view.get("rows") or []:
            new_rows.append({rename.get(k, k): v for k, v in row.items()})
        view = {**view, "columns": columns, "rows": new_rows}

    missing_required = [f for f in profile.required if f not in matched]
    for name in missing_required:
        issues.append(make_error(
            "missing_required_field",
            f"The {profile.standard} profile expects a '{name}' column and could "
            "not find one under any known name.",
            file=file, column=name, rule=f"{profile.standard}.ingest.required_fields",
            hint="Rename the column in the export, or add its name to the "
                 f"'{name}' aliases in rules/*.yaml.",
        ))
    unmatched = [c for c in columns if c not in canonical]
    for col in unmatched:
        issues.append(make_error(
            "unknown_field",
            f"Column '{col}' is not part of the {profile.standard} ingest profile; "
            "it is kept in the dataset but no rule reads it.",
            file=file, column=col, rule=f"{profile.standard}.ingest.fields",
            hint="Map it to a known field in the ingest profile if a rule should read it.",
        ))

    report = {
        "standard": profile.standard,
        "profile_version": profile.version,
        "fields_matched": matched,
        "renamed": rename,
        "unmatched_source_columns": unmatched,
        "missing_required": missing_required,
        "issues": issues,
    }
    view = {**view, "profile": report}
    view["issues"] = list(view.get("issues") or []) + issues
    return view, report


# --- per-row validation -------------------------------------------------------
def validate_view(view: dict, profile: IngestProfile | None, *,
                  file: str | None = None) -> list[dict]:
    """Row-level checks the profile can express, as structured error objects.

    Currently: required fields must be present as columns and non-empty in every
    row. Row numbers are 1-based and count the header row (header = row 1).
    """
    if profile is None:
        return []
    errors: list[dict] = []
    columns = set(view.get("columns") or [])
    header_row = int(view.get("header_row") or 1)
    sheet = view.get("sheet")
    for name in profile.required:
        if name not in columns:
            errors.append(make_error(
                "missing_required_field",
                f"The {profile.standard} profile requires a '{name}' column, "
                "which is not in this file.",
                file=file, sheet=sheet, column=name,
                rule=f"{profile.standard}.ingest.required_fields",
                hint="Rename the column, or add its alias to the ingest profile.",
            ))
    if errors:
        return errors
    for index, row in enumerate(view.get("rows") or []):
        if len(errors) >= MAX_ROW_ERRORS:
            break
        for name in profile.required:
            value = row.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(make_error(
                    "missing_required_field",
                    f"Row {index + 1 + header_row}: required field '{name}' is empty.",
                    file=file, sheet=sheet, row=index + 1 + header_row,
                    column=name, value=value,
                    rule=f"{profile.standard}.ingest.required_fields",
                    hint="Fill the value in the export, or exclude the row.",
                ))
    return errors

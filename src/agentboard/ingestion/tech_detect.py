"""Detect the tech a change operates on — to pick the right structure-space to
enumerate against. CHANGE-SCOPED, not project-scoped.

Design principles (earned tonight):
  1. Look at the CHANGED CODE + ITS TESTS (imports and usage/DDL), NOT the project
     manifest. The manifest is project-scope and overstates; the review is
     change-scope. Imports+usage of the target/test files are precise and
     deterministic.
  2. Imports name the LIBRARY; usage often names the DIALECT. Postgres-specific DDL
     (create policy / RLS, etc.) in the test is strong dialect evidence even when
     the import is an abstraction.
  3. **If detection is not confident, ASK THE HUMAN.** Do not guess. Every false
     positive tonight came from an agent filling a gap with a confident guess. A
     detector that returns "unsure, asking you" is strictly more trustworthy than
     one that always returns an answer. The system must distinguish "I detected X"
     from "I guessed X".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# library/usage signal -> tech. Regexes are matched against imports + file text.
# NOTE: these are TECH identifiers, not bug hints — they say "this is Postgres",
# never "Postgres FK handling is broken".
_LIB_SIGNALS: list[tuple[str, str]] = [
    (r"\bpg-meta\b|\bpostgres-meta\b|\bpg\b|\bpostgres\b|@supabase|pglite|asyncpg|psycopg|Npgsql", "postgres"),
    (r"\bmongodb\b|\bmongoose\b|\bpymongo\b|MongoDB\.Driver", "mongodb"),
    (r"\bmysql2?\b|mysql-connector", "mysql"),
    (r"better-sqlite3|\bsqlite3?\b", "sqlite"),
    (r"mssql|Microsoft\.Data\.SqlClient|tedious", "sqlserver"),
]
# dialect-specific USAGE signals (corroborate the import; DDL that only one dialect has)
_USAGE_SIGNALS: list[tuple[str, str]] = [
    (r"create\s+policy|enable\s+row\s+level\s+security|\brls\b|create\s+schema\b", "postgres"),
]
# ORM / abstraction imports that name a LIBRARY but not the DIALECT -> must resolve
_ORM_SIGNALS: list[tuple[str, str]] = [
    (r"@prisma/client|PrismaClient|\bprisma\b", "prisma"),
    (r"typeorm", "typeorm"),
    (r"sequelize", "sequelize"),
    (r"sqlalchemy|SQLAlchemy", "sqlalchemy"),
]


@dataclass
class Detection:
    tech: str | None = None
    confidence: str = "none"          # high | medium | low | none
    evidence: list[str] = field(default_factory=list)
    ask_human: bool = False
    question: str = ""
    candidates: list[str] = field(default_factory=list)


def _hits(patterns, text) -> dict[str, int]:
    out: dict[str, int] = {}
    for pat, tech in patterns:
        n = len(re.findall(pat, text, re.IGNORECASE))
        if n:
            out[tech] = out.get(tech, 0) + n
    return out


def detect(target_source: str, test_source: str = "") -> Detection:
    """Detect tech from the changed code + its tests. Ask the human if unsure."""
    blob = f"{target_source}\n{test_source}"
    libs = _hits(_LIB_SIGNALS, blob)
    usage = _hits(_USAGE_SIGNALS, test_source or blob)
    orms = _hits(_ORM_SIGNALS, blob)

    ev: list[str] = []
    for t, n in libs.items():
        ev.append(f"library signal: {t} (x{n})")
    for t, n in usage.items():
        ev.append(f"usage/DDL signal: {t} (x{n})")

    concrete = set(libs) | set(usage)

    # exactly one concrete tech, with real evidence -> confident
    if len(concrete) == 1 and (sum(libs.values()) + sum(usage.values())) >= 2:
        t = next(iter(concrete))
        conf = "high" if (t in usage or libs.get(t, 0) >= 2) else "medium"
        return Detection(tech=t, confidence=conf, evidence=ev)

    # multiple concrete techs -> ambiguous, ASK
    if len(concrete) > 1:
        return Detection(
            confidence="low", evidence=ev, ask_human=True, candidates=sorted(concrete),
            question=("The change touches more than one data technology "
                      f"({', '.join(sorted(concrete))}). Which should I review the structure-space against?"),
        )

    # an ORM abstraction but no concrete dialect -> ASK (imports name lib, not dialect)
    if orms and not concrete:
        orm = next(iter(orms))
        return Detection(
            confidence="low", evidence=[f"ORM signal: {orm}"], ask_human=True, candidates=[orm],
            question=(f"This uses {orm}, which abstracts the database dialect. "
                      "Which engine backs it (e.g. postgres / mysql / sqlite)? "
                      "I can check its config if you point me at it."),
        )

    # single concrete tech but weak evidence -> low confidence, still ask to be safe
    if len(concrete) == 1:
        t = next(iter(concrete))
        return Detection(
            tech=t, confidence="low", evidence=ev, ask_human=True, candidates=[t],
            question=(f"I see a weak signal that this is {t}, but I'm not confident. "
                      f"Is this change about {t}?"),
        )

    # nothing -> ASK, never guess
    return Detection(
        confidence="none", ask_human=True,
        question=("I couldn't confidently identify the data technology from the "
                  "changed code and its tests. What technology should I review "
                  "the structure-space against?"),
    )

"""Starting theories: why this person, at this company, right now.

The design's theory generator proposes new ones weekly from reply statistics,
which only exist after sending (stage 4). Until then these three are the seed,
written against the signals the mock can actually produce.
"""

from __future__ import annotations

import json

import psycopg

THEORIES = [
    {
        "name": "fresh funding, hiring for pipeline",
        "hypothesis": ("A company that just raised a round and is hiring sales and marketing people will be "
                       "asked for pipeline numbers before the new hires ramp up. The growth leader needs "
                       "attribution that holds up in the next board meeting."),
        "segment_criteria": {"industry": ["saas", "fintech", "martech"], "seniority": ["c_level", "vp", "director"]},
        "required_variables": ["funding_round", "hiring_pace"],
    },
    {
        "name": "new tool, broken attribution",
        "hypothesis": ("A team that just added a new analytics or CRM tool is mid-migration, and channel "
                       "attribution is the first report that breaks. Right now they are deciding what the "
                       "new setup should measure."),
        "segment_criteria": {"industry": ["saas", "ecommerce", "martech", "fintech"]},
        "required_variables": ["tech_stack_change"],
    },
    {
        "name": "new office, new market",
        "hypothesis": ("A company that just opened an office in a new city needs pipeline in a market where "
                       "it has no history. The growth leader has to show early numbers per region."),
        "segment_criteria": {"size_band": ["51-200", "201-500", "501-1000", "1000+"]},
        "required_variables": ["company_news"],
    },
]


def load_theories(conn: psycopg.Connection) -> int:
    """Insert the seed theories as active. Idempotent by name."""
    n = 0
    with conn.transaction():
        for t in THEORIES:
            cur = conn.execute(
                """INSERT INTO theories (name, hypothesis, segment_criteria, required_variables, status, activated_at)
                   VALUES (%s, %s, %s, %s, 'active', now()) ON CONFLICT (name) DO NOTHING""",
                (t["name"], t["hypothesis"], json.dumps(t["segment_criteria"]), json.dumps(t["required_variables"])),
            )
            n += cur.rowcount
    return n

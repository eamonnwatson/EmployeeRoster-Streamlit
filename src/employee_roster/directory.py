"""Production HRIS lookup. No credentials, sessions, or fallback records are created.

Source: EmployeeRoster.Snowflake/SnowflakeHRISService.cs, QueryChunkAsync.
Keep the termination cutoff and numeric client filter; do not filter inactive
employees out. Unlike ROW_NUMBER, DENSE_RANK retains ties for ambiguity checking.
"""
from __future__ import annotations

import json

from .domain import RosterError, key

DIRECTORY_SQL = """
WITH INPUT_DOMAIN_IDS AS (
    SELECT VALUE::VARCHAR AS DOMAINID
    FROM TABLE(FLATTEN(INPUT => PARSE_JSON(?)))
)
SELECT LOWER(TRIM(E.DOMAINID)) AS DOMAINID,
       TRY_TO_NUMBER(E.CLIENTCODE) AS CLIENTID,
       IFF(E.EMPLOYEESTATUSID = 801, TRUE, FALSE) AS ISACTIVE
FROM ENGAGEHR.ENGAGEHR.GETEMPLOYEES AS E
JOIN INPUT_DOMAIN_IDS AS D ON LOWER(TRIM(E.DOMAINID)) = D.DOMAINID
WHERE IFNULL(E.TERMINATIONDATE, '9999-12-31') >= '2020-01-01'
  AND TRY_TO_NUMBER(E.CLIENTCODE) IS NOT NULL
QUALIFY DENSE_RANK() OVER (
    PARTITION BY LOWER(TRIM(E.DOMAINID)) ORDER BY E.HIREDATE DESC NULLS LAST
) = 1
"""


class SnowflakeDirectory:
    def __init__(self, session):
        self.session = session

    def lookup(self, domains: list[str]) -> dict:
        try:
            if not isinstance(domains, list) or any(
                not isinstance(domain, str) or not domain.strip() or len(domain) > 200
                for domain in domains
            ):
                raise ValueError("Invalid domains")
            normalized = sorted({domain.strip().lower() for domain in domains})
            records = {}
            for start in range(0, len(normalized), 1000):
                batch = normalized[start:start + 1000]
                rows = self.session.sql(DIRECTORY_SQL, params=[json.dumps(batch)]).collect()
                for row in rows:
                    domain = key(row["DOMAINID"])
                    client_id = int(row["CLIENTID"])
                    if not -(2**31) <= client_id < 2**31 or type(row["ISACTIVE"]) is not bool:
                        raise ValueError("Invalid directory record")
                    record = {"client_id": str(client_id), "active": row["ISACTIVE"]}
                    if domain in records and records[domain] != record:
                        raise ValueError("Ambiguous latest hire")
                    records[domain] = record
            return records
        except Exception:
            # Never expose SQL, driver messages, employee records, or query IDs.
            raise RosterError("HRIS lookup failed; no changes were applied.",
                              "DIRECTORY_UNAVAILABLE") from None

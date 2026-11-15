"""Static diagnostic codes that are safe to include in external error reports."""
from enum import StrEnum


class HistoryErrorCode(StrEnum):
    INVALID_COVERAGE = 'Invalid observation coverage'
    HOURLY_MISMATCH = 'Hourly rollup differs from source observations'
    INCOMPLETE_HOURS = 'Incomplete hourly source'
    MISSING_HOURLY = 'Missing hourly aggregates; purge refused'
    MISSING_DAILY = 'Missing daily aggregates; purge refused'


class HistoryIntegrityError(ValueError):
    def __init__(self, code):
        self.code = HistoryErrorCode(code)
        super().__init__(self.code.value)

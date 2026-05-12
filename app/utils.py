import re
from datetime import datetime
from typing import Optional

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def parse_expiry_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None

    date_str = date_str.strip()

    match = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", date_str)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass

    match = re.match(r"(\d{1,2})[\s/-](\w+)[\s/-](\d{2,4})", date_str)
    if match:
        day = int(match.group(1))
        month_str = match.group(2).lower()
        year = int(match.group(3))
        if year < 100:
            year += 2000

        month = MONTH_MAP.get(month_str)
        if month:
            try:
                return datetime(year, month, day)
            except ValueError:
                pass

    match = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", date_str)
    if match:
        d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if y < 100:
            y += 2000
        try:
            return datetime(y, m, d)
        except ValueError:
            try:
                return datetime(y, d, m)
            except ValueError:
                pass

    return None


def days_until_expiry(date_str: Optional[str]) -> Optional[int]:
    expiry = parse_expiry_date(date_str)
    if not expiry:
        return None
    delta = expiry - datetime.utcnow()
    return delta.days

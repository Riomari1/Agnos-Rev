"""
Data quality tool for lead intake validation.

Registered on the IntakeAgent to demonstrate Agno's ``Toolkit`` pattern.
"""

from __future__ import annotations

import re

from agno.tools import Toolkit


class DataQualityTool(Toolkit):
    """Validates lead data fields: email format, company name presence, and
    intra-file duplicate detection."""

    def __init__(self) -> None:
        super().__init__(name="data_quality")
        self.register(self.validate_email)
        self.register(self.validate_company_name)
        self.register(self.check_duplicate)

    @staticmethod
    def validate_email(email: str | None) -> str:
        """Check whether an email address has a valid format.

        Args:
            email: The email string to validate, or None.

        Returns:
            A short verdict: 'valid', 'missing', or 'invalid: <reason>'.
        """
        if not email or not email.strip():
            return "missing"
        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if re.match(pattern, email.strip()):
            return "valid"
        return f"invalid: '{email}' does not match standard email format"

    @staticmethod
    def validate_company_name(name: str | None) -> str:
        """Check whether a company name is present and non-empty.

        Args:
            name: The company name to check, or None.

        Returns:
            'valid' or 'missing'.
        """
        if name and name.strip():
            return "valid"
        return "missing"

    @staticmethod
    def check_duplicate(name: str, seen: list[str]) -> str:
        """Check whether a company name has already been seen in the current batch.

        Args:
            name: The company name to check.
            seen: List of company names already processed (lowercase).

        Returns:
            'duplicate' or 'unique'.
        """
        key = name.strip().lower()
        if key in seen:
            return "duplicate"
        return "unique"

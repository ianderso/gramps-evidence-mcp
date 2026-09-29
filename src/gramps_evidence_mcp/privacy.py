"""Privacy filtering for read tools.

Policy, controlled by ``config.expose_private`` (default False):

* Records carrying the Gramps ``private`` flag are withheld from bulk output.
* Probably-living people are withheld from bulk output. Someone born less than
  :data:`LIVING_THRESHOLD_YEARS` ago with no recorded death is treated as
  possibly alive.
* Direct ``get_person`` access remains allowed. The goal is to prevent
  accidental bulk leakage through searches, tree walks and exports, not to lock
  the owner out of their own tree.

Setting ``expose_private`` True disables all of it.

See ``docs/ARCHITECTURE.md`` for how this fits the rest of the server.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Conventional genealogical "presumed dead" cutoff, in years.
LIVING_THRESHOLD_YEARS = 110


@dataclass
class LivingAssessment:
    """Privacy verdict for a single person.

    Attributes
    ----------
    is_private : bool
        The Gramps record-level private flag.
    is_probably_living : bool
        Born less than :data:`LIVING_THRESHOLD_YEARS` ago with no recorded
        death.
    """

    is_private: bool
    is_probably_living: bool

    @property
    def restricted(self) -> bool:
        """bool: True when the record must be withheld from bulk output."""
        return self.is_private or self.is_probably_living


def assess(
    *,
    private_flag: bool,
    birth_year: int | None,
    death_year: int | None,
    current_year: int,
    died: bool = False,
) -> LivingAssessment:
    """Decide whether a person is private and/or probably living.

    An unknown birth year with no recorded death is treated as probably
    living, erring toward privacy.

    Parameters
    ----------
    private_flag : bool
        The Gramps ``private`` flag on the record.
    birth_year : int or None
        Year of birth, if known.
    death_year : int or None
        Year of death, if known. Any value marks the person as not living.
    current_year : int
        Year to measure against.
    died : bool, optional
        A death is recorded even though its year is not: an undated Death
        event is still a recorded death, and marks the person as not living.

    Returns
    -------
    LivingAssessment
        The verdict for this person.
    """
    probably_living = False
    if death_year is None and not died:
        if birth_year is None:
            probably_living = True
        elif current_year - birth_year < LIVING_THRESHOLD_YEARS:
            probably_living = True
    return LivingAssessment(is_private=bool(private_flag), is_probably_living=probably_living)


def redacted_stub(gramps_id: str | None, handle: str | None) -> dict:
    """Build the placeholder that stands in for a filtered person.

    Exposes only the identifiers needed to fetch the record deliberately, never
    a name or a fact.

    Parameters
    ----------
    gramps_id : str or None
        The person's Gramps id.
    handle : str or None
        The person's internal handle.

    Returns
    -------
    dict
        Stub carrying the identifiers, ``redacted``, and the reason.
    """
    return {
        "gramps_id": gramps_id,
        "handle": handle,
        "redacted": True,
        "reason": "living-or-private",
        "note": "Withheld from bulk output; fetch directly by id if intended.",
    }

"""Builds the text that represents one service in semantic search.

One enriched chunk per service. A service is a few sentences long, so
fixed-length splitting would only separate its name from its preparation
text. The department and provider specialties
are added because a patient's words ("heart scan", "skin check") tend to
match those more closely than the bare service name.

Kept free of database and Temporal imports so the format rules can be
unit-tested on their own; the publish Activity does the querying and
hands the plain values in.
"""

# Separates the department from the specialties, as in the brief's
# "{department} · {specialty}: {service name}" shape.
SPECIALTY_SEPARATOR = " · "


def build_service_chunk_text(
    *,
    department: str,
    specialties: list[str],
    name: str,
    description: str,
    prep_instructions: str,
) -> str:
    """Return the chunk text for one service:
    "{department} · {specialties}: {name}. {description} Preparation: {prep}".

    The same service must always produce the same text, because task 4.9
    skips re-embedding a chunk whose text hash is unchanged. So specialties
    are de-duplicated and sorted here rather than trusting the order the
    database happened to return them in.

    A specialty that only repeats the department name ("Cardiology ·
    Cardiology") is dropped, since it adds no word a patient might search
    for. With no specialty left, the separator goes too:
    "{department}: {name}. ...".
    """
    extra_specialties = sorted(
        {s for s in specialties if s.casefold() != department.casefold()}
    )
    if extra_specialties:
        heading = department + SPECIALTY_SEPARATOR + ", ".join(extra_specialties)
    else:
        heading = department
    return (
        f"{heading}: {_as_sentence(name)} {_as_sentence(description)} "
        f"Preparation: {_as_sentence(prep_instructions)}"
    )


def _as_sentence(text: str) -> str:
    """Trim `text` and end it with a full stop unless it already ends a
    sentence, so a description written without one does not run straight
    into "Preparation:" and one written with one is not doubled.
    """
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."

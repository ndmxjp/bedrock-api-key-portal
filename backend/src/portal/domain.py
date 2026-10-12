"""Email normalization and exact-match domain allow-listing."""


class InvalidEmail(ValueError):
    pass


def normalize_domain(domain: str) -> str:
    d = domain.strip().lower().rstrip(".")
    if not d or any(c.isspace() for c in d) or "@" in d:
        raise InvalidEmail("invalid domain")
    if not d.isascii():
        # No Unicode folding: look-alike/full-width domains are rejected, not normalized.
        raise InvalidEmail("non-ascii domain")
    return d


def normalize_email(raw: str) -> str:
    if not isinstance(raw, str):
        raise InvalidEmail("email must be a string")
    email = raw.strip().lower()
    if email.count("@") != 1 or len(email) > 254:
        raise InvalidEmail("invalid email")
    local, domain = email.split("@")
    if not local or any(c.isspace() or ord(c) < 0x20 for c in local):
        raise InvalidEmail("invalid local part")
    return f"{local}@{normalize_domain(domain)}"


def email_domain(email: str) -> str:
    return normalize_email(email).split("@", 1)[1]


def parse_allowed_domains(value: str) -> frozenset[str]:
    domains = frozenset(normalize_domain(d) for d in value.split(",") if d.strip())
    if not domains:
        raise ValueError("at least one allowed domain is required")
    return domains


def is_allowed_email(email: str, allowed_domains: frozenset[str]) -> bool:
    """Exact (not suffix) domain match: alice@evilcompany.co.jp != company.co.jp."""
    try:
        return email_domain(email) in allowed_domains
    except InvalidEmail:
        return False

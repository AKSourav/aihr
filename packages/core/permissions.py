import re
from enum import StrEnum


class Permission(StrEnum):
    TENANT_CREATE = "tenant:create"
    USER_INVITE = "user:invite"
    JOB_READ = "job:read"
    JOB_WRITE = "job:write"
    CANDIDATE_READ = "candidate:read"
    CANDIDATE_WRITE = "candidate:write"
    CALL_CREATE = "call:create"
    CALL_READ = "call:read"
    TRANSCRIPT_READ = "transcript:read"
    SCORE_READ = "score:read"


ADMIN = "admin"
RECRUITER = "recruiter"
VIEWER = "viewer"

OWNER = "owner"
MEMBER = "member"

ROLE_PERMISSIONS: dict[str, list[Permission]] = {
    ADMIN: [
        Permission.USER_INVITE,
        Permission.JOB_READ,
        Permission.JOB_WRITE,
        Permission.CANDIDATE_READ,
        Permission.CANDIDATE_WRITE,
        Permission.CALL_CREATE,
        Permission.CALL_READ,
        Permission.TRANSCRIPT_READ,
        Permission.SCORE_READ,
    ],
    RECRUITER: [
        Permission.JOB_READ,
        Permission.CANDIDATE_READ,
        Permission.CANDIDATE_WRITE,
        Permission.CALL_CREATE,
        Permission.CALL_READ,
        Permission.TRANSCRIPT_READ,
        Permission.SCORE_READ,
    ],
    VIEWER: [
        Permission.JOB_READ,
        Permission.CANDIDATE_READ,
        Permission.CALL_READ,
        Permission.SCORE_READ,
    ],
}

ACCOUNT_ROLE_PERMISSIONS: dict[str, list[Permission]] = {
    OWNER: [Permission.TENANT_CREATE, Permission.USER_INVITE],
    MEMBER: [],
}

SENSITIVE: frozenset[Permission] = frozenset(
    {
        Permission.TRANSCRIPT_READ,
        Permission.CALL_CREATE,
        Permission.CANDIDATE_WRITE,
        Permission.JOB_WRITE,
        Permission.USER_INVITE,
        Permission.TENANT_CREATE,
    }
)

ROUTES: list[tuple[str, str, Permission]] = [
    ("POST", "/tenants/{org_id}/calls", Permission.CALL_CREATE),
    ("POST", "/tenants/{org_id}/applications/{application_id}/screenings", Permission.CALL_CREATE),
    ("GET", "/tenants/{org_id}/applications", Permission.SCORE_READ),
    ("GET", "/tenants/{org_id}/applications/{application_id}", Permission.SCORE_READ),
    ("GET", "/tenants/{org_id}/calls/{call_id}", Permission.CALL_READ),
    ("GET", "/tenants/{org_id}/calls/{call_id}/summary", Permission.SCORE_READ),
    ("GET", "/tenants/{org_id}/calls/{call_id}/transcript", Permission.TRANSCRIPT_READ),
]

ACCOUNT_ROUTES: list[tuple[str, str, Permission]] = [
    ("POST", "/accounts/{account_id}/invitations", Permission.USER_INVITE),
    ("GET", "/accounts/{account_id}/members", Permission.USER_INVITE),
    ("PATCH", "/accounts/{account_id}/members/{user_id}", Permission.USER_INVITE),
    ("DELETE", "/accounts/{account_id}/members/{user_id}", Permission.USER_INVITE),
    ("POST", "/accounts/{account_id}/tenants", Permission.TENANT_CREATE),
]

PLATFORM_ROUTES: list[tuple[str, str]] = [
    ("POST", "/platform/break-glass"),
]

# Routes any authenticated caller may reach; they only ever describe the caller themselves.
SELF_ROUTES: list[tuple[str, str]] = [
    ("GET", "/me"),
]


def path_pattern(template: str, capture: str = "org_id") -> str:
    escaped = re.escape(template)
    escaped = escaped.replace(re.escape("{" + capture + "}"), "([^/]+)")
    escaped = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", escaped)
    return f"^{escaped}$"


def route_table() -> list[dict]:
    tenant = [
        {
            "method": method,
            "pattern": path_pattern(template),
            "permission": str(permission),
            "scope": "tenant",
        }
        for method, template, permission in ROUTES
    ]
    account = [
        {
            "method": method,
            "pattern": path_pattern(template, capture="account_id"),
            "permission": str(permission),
            "scope": "account",
        }
        for method, template, permission in ACCOUNT_ROUTES
    ]
    return tenant + account


def platform_route_table() -> list[dict]:
    return [
        {"method": method, "pattern": path_pattern(template)}
        for method, template in PLATFORM_ROUTES
    ]


def self_route_table() -> list[dict]:
    return [
        {"method": method, "pattern": path_pattern(template)}
        for method, template in SELF_ROUTES
    ]


def role_permission_table() -> dict[str, list[str]]:
    return {role: [str(p) for p in perms] for role, perms in ROLE_PERMISSIONS.items()}


def grants(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, [])


def account_grants(account_role: str, permission: Permission) -> bool:
    return permission in ACCOUNT_ROLE_PERMISSIONS.get(account_role, [])

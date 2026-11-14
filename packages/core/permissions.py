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
]


def path_pattern(template: str) -> str:
    escaped = re.escape(template)
    escaped = escaped.replace(r"\{org_id\}", "([^/]+)")
    escaped = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", escaped)
    return f"^{escaped}$"


def route_table() -> list[dict]:
    return [
        {"method": method, "pattern": path_pattern(template), "permission": str(permission)}
        for method, template, permission in ROUTES
    ]


def role_permission_table() -> dict[str, list[str]]:
    table = {role: [str(p) for p in perms] for role, perms in ROLE_PERMISSIONS.items()}
    for role, perms in ACCOUNT_ROLE_PERMISSIONS.items():
        table[f"account:{role}"] = [str(p) for p in perms]
    return table


def grants(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, [])


def account_grants(account_role: str, permission: Permission) -> bool:
    return permission in ACCOUNT_ROLE_PERMISSIONS.get(account_role, [])

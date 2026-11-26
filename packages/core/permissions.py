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


OWNER = "owner"
ADMIN = "admin"
RECRUITER = "recruiter"
VIEWER = "viewer"

ROLE_PERMISSIONS: dict[str, list[Permission]] = {
    OWNER: [
        Permission.TENANT_CREATE,
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
    ("POST", "/tenants/{org_id}/jobs", Permission.JOB_WRITE),
    ("GET", "/tenants/{org_id}/jobs", Permission.JOB_READ),
    ("POST", "/tenants/{org_id}/jobs/drafts", Permission.JOB_WRITE),
    ("GET", "/tenants/{org_id}/jobs/{job_id}", Permission.JOB_READ),
    ("GET", "/tenants/{org_id}/jobs/{job_id}/provider-settings", Permission.JOB_WRITE),
    ("PUT", "/tenants/{org_id}/jobs/{job_id}/provider-settings", Permission.JOB_WRITE),
    ("POST", "/tenants/{org_id}/candidates", Permission.CANDIDATE_WRITE),
    ("POST", "/tenants/{org_id}/candidates/import", Permission.CANDIDATE_WRITE),
    ("POST", "/tenants/{org_id}/applications", Permission.CANDIDATE_WRITE),
    ("POST", "/tenants/{org_id}/candidates/resumes", Permission.CANDIDATE_WRITE),
    ("POST", "/tenants/{org_id}/candidates/{candidate_id}/resume", Permission.CANDIDATE_WRITE),
    ("GET", "/tenants/{org_id}/candidates/{candidate_id}/resume", Permission.CANDIDATE_READ),
    ("GET", "/tenants/{org_id}/candidates/{candidate_id}/resume/file", Permission.CANDIDATE_READ),
    ("GET", "/tenants/{org_id}/jobs/{job_id}/resumes", Permission.CANDIDATE_READ),
    ("GET", "/tenants/{org_id}/jobs/{job_id}/candidates", Permission.SCORE_READ),
    ("GET", "/tenants/{org_id}/jobs/{job_id}/shortlist-rules", Permission.JOB_READ),
    ("PUT", "/tenants/{org_id}/jobs/{job_id}/shortlist-rules", Permission.JOB_WRITE),
    ("POST", "/tenants/{org_id}/test-calls", Permission.CALL_CREATE),
    ("POST", "/tenants/{org_id}/jobs/{job_id}/candidate-summary", Permission.CANDIDATE_WRITE),
    ("POST", "/tenants/{org_id}/assistant/messages", Permission.CANDIDATE_READ),
    ("GET", "/tenants/{org_id}/assistant/messages", Permission.CANDIDATE_READ),
    (
        "POST",
        "/tenants/{org_id}/assistant/actions/{action_id}/confirm",
        Permission.CALL_CREATE,
    ),
    (
        "DELETE",
        "/tenants/{org_id}/assistant/conversations/{conversation_id}",
        Permission.CANDIDATE_READ,
    ),
    ("POST", "/tenants/{org_id}/mcp", Permission.CANDIDATE_READ),
]

ACCOUNT_ROUTES: list[tuple[str, str, Permission]] = [
    ("GET", "/accounts/{account_id}/secrets", Permission.TENANT_CREATE),
    ("POST", "/accounts/{account_id}/secrets", Permission.TENANT_CREATE),
    ("PUT", "/accounts/{account_id}/secrets/{secret_id}", Permission.TENANT_CREATE),
    ("DELETE", "/accounts/{account_id}/secrets/{secret_id}", Permission.TENANT_CREATE),
    ("GET", "/accounts/{account_id}/provider-settings", Permission.TENANT_CREATE),
    ("PUT", "/accounts/{account_id}/provider-settings", Permission.TENANT_CREATE),
    ("GET", "/accounts/{account_id}/tenants/{org_id}/provider-settings", Permission.TENANT_CREATE),
    ("PUT", "/accounts/{account_id}/tenants/{org_id}/provider-settings", Permission.TENANT_CREATE),
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
    ("POST", "/organizations"),
    ("GET", "/provider-catalogue"),
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

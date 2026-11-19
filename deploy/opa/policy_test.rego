package aihr.authz_test

import rego.v1

import data.aihr.authz

memberships := {
	"sub-recruiter": {"org-a": "recruiter"},
	"sub-viewer": {"org-a": "viewer"},
	"sub-admin": {"org-a": "admin"},
}

account_access := {
	"sub-owner": ["acc-1"],
	"sub-admin": ["acc-1"],
}

platform_admins := ["sub-root"]

role_permissions := {
	"recruiter": ["call:create", "call:read"],
	"viewer": ["call:read"],
	"admin": ["call:create", "call:read", "user:invite"],
}

routes := [
	{"method": "POST", "pattern": "^/tenants/([^/]+)/calls$", "permission": "call:create", "scope": "tenant"},
	{"method": "GET", "pattern": "^/tenants/([^/]+)/calls/[^/]+$", "permission": "call:read", "scope": "tenant"},
	{"method": "POST", "pattern": "^/accounts/([^/]+)/invitations$", "permission": "user:invite", "scope": "account"},
	{"method": "GET", "pattern": "^/accounts/([^/]+)/members$", "permission": "user:invite", "scope": "account"},
]

platform_routes := [{"method": "POST", "pattern": "^/platform/break\\-glass$"}]

request(sub, method, path) := {"attributes": {"request": {"http": {
	"headers": {"x-jwt-sub": sub},
	"method": method,
	"path": path,
}}}}

test_recruiter_may_create_a_call_in_their_tenant if {
	authz.allow with input as request("sub-recruiter", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_viewer_may_not_create_a_call if {
	not authz.allow with input as request("sub-viewer", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_viewer_may_read_a_call if {
	authz.allow with input as request("sub-viewer", "GET", "/tenants/org-a/calls/c1")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_recruiter_may_not_act_on_another_tenant if {
	not authz.allow with input as request("sub-recruiter", "POST", "/tenants/org-b/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_is_allowed if {
	authz.allow with input as request("sub-root", "POST", "/tenants/org-b/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_unknown_subject_is_denied if {
	not authz.allow with input as request("sub-nobody", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_unmapped_route_is_denied if {
	not authz.allow with input as request("sub-recruiter", "DELETE", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_query_string_does_not_defeat_matching if {
	authz.allow with input as request("sub-viewer", "GET", "/tenants/org-a/calls/c1?verbose=1")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_may_open_break_glass if {
	authz.allow with input as request("sub-root", "POST", "/platform/break-glass")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_recruiter_may_not_open_break_glass if {
	not authz.allow with input as request("sub-recruiter", "POST", "/platform/break-glass")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_is_denied_on_an_unmapped_path if {
	not authz.allow with input as request("sub-root", "GET", "/internal/opa/bundle.tar.gz")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_account_owner_may_invite_on_their_account if {
	authz.allow with input as request("sub-owner", "POST", "/accounts/acc-1/invitations")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_account_owner_may_not_reach_another_account if {
	not authz.allow with input as request("sub-owner", "POST", "/accounts/acc-2/invitations")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_tenant_admin_reaches_the_account_of_their_org if {
	authz.allow with input as request("sub-admin", "GET", "/accounts/acc-1/members")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_recruiter_may_not_reach_an_account_route if {
	not authz.allow with input as request("sub-recruiter", "GET", "/accounts/acc-1/members")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_a_tenant_membership_does_not_satisfy_an_account_route if {
	not authz.allow with input as request("sub-admin", "GET", "/accounts/org-a/members")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_account_access_does_not_grant_tenant_routes if {
	not authz.allow with input as request("sub-owner", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_is_edge_allowed_on_account_routes if {
	authz.allow with input as request("sub-root", "GET", "/accounts/acc-9/members")
		with data.memberships as memberships
		with data.account_access as account_access
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

self_routes := [{"method": "GET", "pattern": "^/me$"}]

test_any_verified_subject_may_read_me if {
	authz.allow with input as request("sub-nobody", "GET", "/me")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
		with data.self_routes as self_routes
}

test_me_requires_a_subject if {
	not authz.allow with input as request("", "GET", "/me")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
		with data.self_routes as self_routes
}

test_self_routes_allow_only_their_method if {
	not authz.allow with input as request("sub-nobody", "POST", "/me")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
		with data.self_routes as self_routes
}

test_policy_works_without_self_routes_in_the_bundle if {
	authz.allow with input as request("sub-recruiter", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

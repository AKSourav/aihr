package aihr.authz_test

import rego.v1

import data.aihr.authz

memberships := {
	"sub-recruiter": {"org-a": "recruiter"},
	"sub-viewer": {"org-a": "viewer"},
}

platform_admins := ["sub-root"]

role_permissions := {
	"recruiter": ["call:create", "call:read"],
	"viewer": ["call:read"],
}

routes := [
	{"method": "POST", "pattern": "^/tenants/([^/]+)/calls$", "permission": "call:create"},
	{"method": "GET", "pattern": "^/tenants/([^/]+)/calls/[^/]+$", "permission": "call:read"},
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
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_viewer_may_not_create_a_call if {
	not authz.allow with input as request("sub-viewer", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_viewer_may_read_a_call if {
	authz.allow with input as request("sub-viewer", "GET", "/tenants/org-a/calls/c1")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_recruiter_may_not_act_on_another_tenant if {
	not authz.allow with input as request("sub-recruiter", "POST", "/tenants/org-b/calls")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_is_allowed if {
	authz.allow with input as request("sub-root", "POST", "/tenants/org-b/calls")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_unknown_subject_is_denied if {
	not authz.allow with input as request("sub-nobody", "POST", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_unmapped_route_is_denied if {
	not authz.allow with input as request("sub-recruiter", "DELETE", "/tenants/org-a/calls")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_query_string_does_not_defeat_matching if {
	authz.allow with input as request("sub-viewer", "GET", "/tenants/org-a/calls/c1?verbose=1")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_may_open_break_glass if {
	authz.allow with input as request("sub-root", "POST", "/platform/break-glass")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_recruiter_may_not_open_break_glass if {
	not authz.allow with input as request("sub-recruiter", "POST", "/platform/break-glass")
		with data.memberships as memberships
		with data.platform_admins as platform_admins
		with data.role_permissions as role_permissions
		with data.routes as routes
		with data.platform_routes as platform_routes
}

test_platform_admin_is_denied_on_an_unmapped_path if {
	not authz.allow with input as request("sub-root", "GET", "/internal/opa/bundle.tar.gz")
		with data.memberships as memberships
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

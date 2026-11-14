package aihr.authz

import rego.v1

default allow := false

http_request := input.attributes.request.http

subject := http_request.headers["x-jwt-sub"]

method := http_request.method

path := split(http_request.path, "?")[0]

matched contains r if {
	some r in data.routes
	r.method == method
	regex.match(r.pattern, path)
}

tenant_of(pattern) := captured if {
	captures := regex.find_all_string_submatch_n(pattern, path, 1)
	captured := captures[0][1]
}

platform_matched contains r if {
	some r in data.platform_routes
	r.method == method
	regex.match(r.pattern, path)
}

allow if {
	subject in data.platform_admins
	count(matched) > 0
}

allow if {
	subject in data.platform_admins
	count(platform_matched) > 0
}

allow if {
	some r in matched
	role := data.memberships[subject][tenant_of(r.pattern)]
	r.permission in data.role_permissions[role]
}

reason := "no matching route" if {
	count(matched) == 0
	count(platform_matched) == 0
} else := "allowed" if {
	allow
} else := "denied"

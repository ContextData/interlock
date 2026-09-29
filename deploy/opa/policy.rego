package interlock.authz

default allow = false

# Analysts can read any data source
allow {
    input.operation == "read"
    input.identity.roles[_] == "analyst"
}

# Admins have full access
allow {
    input.identity.roles[_] == "admin"
}

# Engineers can read and write
allow {
    input.operation == "read"
    input.identity.roles[_] == "engineer"
}

allow {
    input.operation == "write"
    input.identity.roles[_] == "engineer"
}

# Deny reason for debugging
reason = msg {
    not allow
    msg := "No matching OPA rule - access denied"
}

reason = msg {
    allow
    msg := "Access granted by OPA policy"
}

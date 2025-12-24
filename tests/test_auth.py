from tests.conftest import PASSWORD, auth, create_org, join_code, register_student


def login(client, email, password=PASSWORD):
    return client.post("/api/v1/auth/login", data={"username": email, "password": password})


def test_org_signup_makes_caller_admin(client):
    headers = create_org(client)
    me = client.get("/api/v1/users/me", headers=headers).json()
    assert me["role"] == "admin"
    assert me["email"] == "admin@school.edu"
    assert "hashed_password" not in me and "password" not in me

    org = client.get("/api/v1/organizations/me", headers=headers).json()
    assert org["name"] == "Springfield High"
    assert len(org["join_code"]) == 8


def test_student_joins_with_code_case_insensitive(client):
    admin = create_org(client)
    code = join_code(client, admin)
    student = register_student(client, code.lower())
    me = client.get("/api/v1/users/me", headers=student).json()
    admin_me = client.get("/api/v1/users/me", headers=admin).json()
    assert me["role"] == "student"
    assert me["org_id"] == admin_me["org_id"]


def test_invalid_join_code(client):
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": "NOPE1234", "full_name": "X", "email": "x@x.io", "password": PASSWORD},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid join code"


def test_duplicate_email_conflicts_even_across_case(client):
    admin = create_org(client)
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": join_code(client, admin), "full_name": "X", "email": "ADMIN@school.edu", "password": PASSWORD},
    )
    assert r.status_code == 409


def test_login_success_and_failures(client):
    create_org(client)
    ok = login(client, "Admin@School.edu")
    assert ok.status_code == 200
    assert ok.json()["token_type"] == "bearer"
    assert client.get("/api/v1/users/me", headers=auth(ok.json()["access_token"])).status_code == 200

    wrong_pw = login(client, "admin@school.edu", "wrong-password")
    unknown = login(client, "ghost@school.edu")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()


def test_weak_password_rejected(client):
    r = client.post(
        "/api/v1/auth/organizations",
        json={"organization_name": "Org", "full_name": "A", "email": "a@a.io", "password": "short"},
    )
    assert r.status_code == 422


def test_bad_tokens_rejected(client):
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/users/me", headers=auth("garbage")).status_code == 401


def test_role_management(client, school):
    student_id = client.get("/api/v1/users/me", headers=school["student"]).json()["id"]
    admin_id = client.get("/api/v1/users/me", headers=school["admin"]).json()["id"]

    # Students and teachers can't promote anyone.
    assert client.patch(f"/api/v1/users/{student_id}/role", json={"role": "admin"}, headers=school["student"]).status_code == 403
    assert client.patch(f"/api/v1/users/{student_id}/role", json={"role": "admin"}, headers=school["teacher"]).status_code == 403

    # Admin can't lock themselves out.
    assert client.patch(f"/api/v1/users/{admin_id}/role", json={"role": "student"}, headers=school["admin"]).status_code == 400

    r = client.patch(f"/api/v1/users/{student_id}/role", json={"role": "teacher"}, headers=school["admin"])
    assert r.status_code == 200 and r.json()["role"] == "teacher"
    # Takes effect on the existing token immediately.
    assert client.get("/api/v1/organizations/me", headers=school["student"]).status_code == 200


def test_staff_can_list_org_users_only(client, school):
    other_admin = create_org(client, email="boss@other.edu", name="Other")
    users = client.get("/api/v1/users", headers=school["teacher"]).json()
    assert {u["role"] for u in users} == {"admin", "teacher", "student"}
    assert len(client.get("/api/v1/users", headers=other_admin).json()) == 1
    assert client.get("/api/v1/users", headers=school["student"]).status_code == 403


def test_cannot_change_role_in_other_org(client, school):
    other_admin = create_org(client, email="boss@other.edu", name="Other")
    student_id = client.get("/api/v1/users/me", headers=school["student"]).json()["id"]
    r = client.patch(f"/api/v1/users/{student_id}/role", json={"role": "admin"}, headers=other_admin)
    assert r.status_code == 404


def test_rotate_join_code(client, school):
    old = school["code"]
    assert client.post("/api/v1/organizations/me/join-code", headers=school["teacher"]).status_code == 403
    new = client.post("/api/v1/organizations/me/join-code", headers=school["admin"]).json()["join_code"]
    assert new != old
    r = client.post(
        "/api/v1/auth/register",
        json={"join_code": old, "full_name": "Late", "email": "late@school.edu", "password": PASSWORD},
    )
    assert r.status_code == 400
    register_student(client, new, "late@school.edu")

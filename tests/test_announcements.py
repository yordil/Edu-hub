from tests.conftest import create_org, register_student

URL = "/api/v1/announcements"


def post(client, headers, title, body="Details", pinned=False):
    return client.post(URL, json={"title": title, "body": body, "pinned": pinned}, headers=headers)


def titles(client, headers):
    return [a["title"] for a in client.get(URL, headers=headers).json()]


def test_staff_post_students_read(client, school):
    r = post(client, school["teacher"], "Quiz Friday")
    assert r.status_code == 201
    body = r.json()
    assert body["scope"] == "school" and body["author_name"] == "Sam Student" and body["pinned"] is False

    assert post(client, school["student"], "Hi").status_code == 403
    assert titles(client, school["student"]) == ["Quiz Friday"]


def test_announcements_are_school_private(client, school):
    post(client, school["admin"], "Springfield only")
    other = create_org(client, email="boss@other.edu", name="Other")
    assert titles(client, other) == []
    assert post(client, other, "Other only").status_code == 201
    assert titles(client, school["student"]) == ["Springfield only"]


def test_pinned_first_then_newest(client, school):
    post(client, school["admin"], "old")
    post(client, school["admin"], "pinned", pinned=True)
    post(client, school["admin"], "new")
    assert titles(client, school["student"]) == ["pinned", "new", "old"]


def test_validation(client, school):
    assert post(client, school["admin"], "").status_code == 422
    assert post(client, school["admin"], "x" * 161).status_code == 422


def test_delete_permissions(client, school):
    mine = post(client, school["teacher"], "teacher's").json()["id"]
    admins = post(client, school["admin"], "admin's").json()["id"]

    assert client.delete(f"{URL}/{admins}", headers=school["teacher"]).status_code == 403
    assert client.delete(f"{URL}/{mine}", headers=school["student"]).status_code == 403
    assert client.delete(f"{URL}/{mine}", headers=school["teacher"]).status_code == 204

    other = create_org(client, email="boss@other.edu", name="Other")
    assert client.delete(f"{URL}/{admins}", headers=other).status_code == 404
    assert client.delete(f"{URL}/{admins}", headers=school["admin"]).status_code == 204
    assert titles(client, school["student"]) == []


def test_platform_wide_announcements(client, school, platform_admin):
    r = client.post("/api/v1/platform/announcements", json={"title": "Maintenance Sunday", "body": "Back by 6am."}, headers=platform_admin)
    assert r.status_code == 201 and r.json()["scope"] == "platform"
    ann_id = r.json()["id"]

    other = create_org(client, email="boss@other.edu", name="Other")
    newcomer = register_student(client, school["code"], "new@school.edu")
    for headers in (school["student"], other, newcomer):
        assert titles(client, headers) == ["Maintenance Sunday"]

    assert client.delete(f"{URL}/{ann_id}", headers=school["admin"]).status_code == 403
    assert client.delete(f"{URL}/{ann_id}", headers=platform_admin).status_code == 204
    assert titles(client, other) == []

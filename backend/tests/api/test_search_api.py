"""The HTTP layer over search: /search/*, /metadata/schemas, and the bulk edits.

`storage/search_query.py`'s filter builder already has its own tests; what is
untested there is everything between the query string and that builder --
whether `meta=key=value` survives the URL, whether a bad sort or an unknown
format kind produces a 422 rather than a 500, and whether the two bulk-write
routes refuse an empty body instead of issuing a no-op update.

The two `/metadata/schemas` routes are deliberately unscoped (see the docstring
on `list_schemas`), which is why they are the only requests here sent without a
profile header.
"""

import pytest

from app.models import FormatInfo, FormatKind
from tests.services.helpers import make_object, make_project

pytestmark = [
    pytest.mark.usefixtures("beanie_models"),
    pytest.mark.asyncio(loop_scope="module"),
]


async def _fastq(project, name, **kwargs):
    """An object the search routes will actually match on kind.

    `make_object` leaves `format` at its UNKNOWN default, which is fine for the
    deletion-cascade tests it was written for but makes every `kind=` filter
    here match nothing.
    """
    obj = await make_object(project, name, **kwargs)
    obj.format = FormatInfo(kind=FormatKind.FASTQ)
    await obj.save()
    return obj


class TestSearchObjects:
    async def test_finds_the_callers_objects(self, client, two_profiles):
        project = await make_project("searchable", owner=two_profiles["a"].owner_id())
        await _fastq(project, "reads_R1.fastq.gz")

        resp = await client.get(
            "/api/v1/search/objects", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "reads_R1.fastq.gz" in [o["name"] for o in body["objects"]]
        assert body["total"] >= 1
        assert body["has_more"] is False

    async def test_does_not_find_another_profiles_objects(self, client, two_profiles):
        project = await make_project("private", owner=two_profiles["a"].owner_id())
        await _fastq(project, "not_yours.fastq")

        resp = await client.get(
            "/api/v1/search/objects", headers=two_profiles["b_headers"]
        )

        assert "not_yours.fastq" not in [o["name"] for o in resp.json()["objects"]]

    async def test_matches_a_filename_substring(self, client, two_profiles):
        project = await make_project("substring", owner=two_profiles["a"].owner_id())
        await _fastq(project, "sampleXYZ_R1.fastq")
        await _fastq(project, "controlABC_R1.fastq")

        resp = await client.get(
            "/api/v1/search/objects?q=sampleXYZ", headers=two_profiles["a_headers"]
        )

        names = [o["name"] for o in resp.json()["objects"]]
        assert names == ["sampleXYZ_R1.fastq"]

    async def test_narrows_to_one_project(self, client, two_profiles):
        owner = two_profiles["a"].owner_id()
        wanted = await make_project("wanted", owner=owner)
        other = await make_project("other", owner=owner)
        await _fastq(wanted, "in_scope.fastq")
        await _fastq(other, "out_of_scope.fastq")

        resp = await client.get(
            f"/api/v1/search/objects?project_id={wanted.id}",
            headers=two_profiles["a_headers"],
        )

        names = [o["name"] for o in resp.json()["objects"]]
        assert "in_scope.fastq" in names
        assert "out_of_scope.fastq" not in names

    async def test_parses_a_metadata_filter_from_the_query_string(
        self, client, two_profiles
    ):
        """The `key=value` syntax is what makes a search shareable as a URL, so
        it has to survive the round trip through the query parameter."""
        project = await make_project("meta filter", owner=two_profiles["a"].owner_id())
        tagged = await _fastq(project, "batch7.fastq")
        tagged.metadata = {"batch": "seven"}
        await tagged.save()
        await _fastq(project, "unbatched.fastq")

        resp = await client.get(
            "/api/v1/search/objects?meta=batch%3Dseven",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        names = [o["name"] for o in resp.json()["objects"]]
        assert names == ["batch7.fastq"]

    async def test_filters_by_tag(self, client, two_profiles):
        project = await make_project("tag filter", owner=two_profiles["a"].owner_id())
        tagged = await _fastq(project, "tagged.fastq")
        tagged.tags = ["keeper"]
        await tagged.save()
        await _fastq(project, "untagged.fastq")

        resp = await client.get(
            "/api/v1/search/objects?tag=keeper", headers=two_profiles["a_headers"]
        )

        names = [o["name"] for o in resp.json()["objects"]]
        assert names == ["tagged.fastq"]

    async def test_rejects_a_limit_over_the_service_cap(self, client, two_profiles):
        """Declared as `le=MAX_LIMIT`, so an oversized page is refused at the
        edge rather than silently clamped somewhere further in."""
        resp = await client.get(
            "/api/v1/search/objects?limit=100000", headers=two_profiles["a_headers"]
        )

        assert resp.status_code == 422

    async def test_requires_a_profile(self, client):
        resp = await client.get("/api/v1/search/objects")

        assert resp.status_code in (400, 401, 403, 422)


class TestFacets:
    async def test_counts_the_callers_objects_by_kind(self, client, two_profiles):
        project = await make_project("facets", owner=two_profiles["a"].owner_id())
        await _fastq(project, "facet_a.fastq")
        await _fastq(project, "facet_b.fastq")

        resp = await client.get(
            f"/api/v1/search/facets?project_id={project.id}",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        formats = {e["value"]: e["count"] for e in resp.json()["formats"]}
        assert formats.get("fastq") == 2

    async def test_a_second_profile_sees_none_of_them(self, client, two_profiles):
        project = await make_project("facets b", owner=two_profiles["a"].owner_id())
        await _fastq(project, "hidden.fastq")

        resp = await client.get(
            f"/api/v1/search/facets?project_id={project.id}",
            headers=two_profiles["b_headers"],
        )

        assert resp.status_code == 200
        assert resp.json()["formats"] == []


class TestMetadataValues:
    async def test_lists_the_distinct_values_for_one_key(self, client, two_profiles):
        project = await make_project("values", owner=two_profiles["a"].owner_id())
        for name, batch in (("v1.fastq", "alpha"), ("v2.fastq", "beta")):
            obj = await _fastq(project, name)
            obj.metadata = {"run_batch": batch}
            await obj.save()

        resp = await client.get(
            f"/api/v1/search/metadata-values/run_batch?project_id={project.id}",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["key"] == "run_batch"
        assert sorted(v["value"] for v in body["values"]) == ["alpha", "beta"]

    async def test_an_unknown_key_is_an_empty_list_not_an_error(
        self, client, two_profiles
    ):
        """Metadata keys are an open vocabulary, so a key nobody has used is a
        normal answer rather than a 404."""
        resp = await client.get(
            "/api/v1/search/metadata-values/nobody_uses_this",
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200
        assert resp.json()["values"] == []


class TestSchemas:
    async def test_lists_every_known_format(self, client):
        resp = await client.get("/api/v1/metadata/schemas")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "common" in body
        assert "fastq" in body["schemas"]
        assert "unknown" not in body["schemas"]

    async def test_needs_no_profile_header(self, client):
        """Built from the enums, identical for every profile -- requiring a
        header here would break the metadata editor's field pickers for a
        client that has not resolved a profile yet."""
        resp = await client.get("/api/v1/metadata/schemas")

        assert resp.status_code == 200

    async def test_returns_one_formats_fields(self, client):
        resp = await client.get("/api/v1/metadata/schemas/fastq")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["kind"] == "fastq"
        keys = {f["key"] for group in body["groups"] for f in group["fields"]}
        assert "sample_id" in keys

    async def test_422s_on_an_unknown_kind_and_says_which_are_known(self, client):
        resp = await client.get("/api/v1/metadata/schemas/not_a_format")

        assert resp.status_code == 422, resp.text
        assert "fastq" in resp.json()["details"]["known"]

    async def test_422s_on_an_unknown_role(self, client):
        resp = await client.get("/api/v1/metadata/schemas/fastq?role=not_a_role")

        assert resp.status_code == 422, resp.text
        assert "known" in resp.json()["details"]


class TestBulkMetadata:
    async def test_merges_values_into_existing_metadata(self, client, two_profiles):
        """Merging rather than replacing is the whole contract: assigning one
        field must not erase the others the file already carries."""
        project = await make_project("bulk meta", owner=two_profiles["a"].owner_id())
        obj = await _fastq(project, "bulk1.fastq")
        obj.metadata = {"keep_me": "yes"}
        await obj.save()

        resp = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={"object_ids": [str(obj.id)], "set": {"added": "value"}},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["matched"] == 1
        await obj.sync()
        assert obj.metadata == {"keep_me": "yes", "added": "value"}

    async def test_unsets_a_key(self, client, two_profiles):
        project = await make_project("bulk unset", owner=two_profiles["a"].owner_id())
        obj = await _fastq(project, "bulk2.fastq")
        obj.metadata = {"drop_me": "x", "keep_me": "y"}
        await obj.save()

        resp = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={"object_ids": [str(obj.id)], "unset": ["drop_me"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        await obj.sync()
        assert obj.metadata == {"keep_me": "y"}

    async def test_refuses_a_body_with_neither_set_nor_unset(self, client, two_profiles):
        """Without the guard this is an update document containing only a
        timestamp bump -- a write that reports success and changes nothing."""
        project = await make_project("bulk empty", owner=two_profiles["a"].owner_id())
        obj = await _fastq(project, "bulk3.fastq")

        resp = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={"object_ids": [str(obj.id)]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text

    async def test_refuses_an_empty_id_list(self, client, two_profiles):
        resp = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={"object_ids": [], "set": {"a": "b"}},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422

    async def test_refuses_the_whole_batch_when_one_id_is_another_owners(
        self, client, two_profiles
    ):
        """A short count is a worse answer than a refusal: the caller would have
        no way to tell which rows were skipped."""
        a_project = await make_project("mine", owner=two_profiles["a"].owner_id())
        b_project = await make_project("theirs", owner=two_profiles["b"].owner_id())
        mine = await _fastq(a_project, "mine.fastq")
        theirs = await _fastq(b_project, "theirs.fastq")

        resp = await client.post(
            "/api/v1/objects/bulk-metadata",
            json={
                "object_ids": [str(mine.id), str(theirs.id)],
                "set": {"batch": "1"},
            },
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404, resp.text
        await mine.sync()
        assert mine.metadata == {}


class TestBulkTags:
    async def test_adds_a_tag(self, client, two_profiles):
        project = await make_project("tags add", owner=two_profiles["a"].owner_id())
        obj = await _fastq(project, "tag1.fastq")

        resp = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)], "add": ["batch7"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        await obj.sync()
        assert obj.tags == ["batch7"]

    async def test_removes_a_tag(self, client, two_profiles):
        project = await make_project("tags remove", owner=two_profiles["a"].owner_id())
        obj = await _fastq(project, "tag2.fastq")
        obj.tags = ["keep", "drop"]
        await obj.save()

        resp = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)], "remove": ["drop"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 200, resp.text
        await obj.sync()
        assert obj.tags == ["keep"]

    async def test_refuses_a_body_with_neither_add_nor_remove(
        self, client, two_profiles
    ):
        project = await make_project("tags empty", owner=two_profiles["a"].owner_id())
        obj = await _fastq(project, "tag3.fastq")

        resp = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(obj.id)]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 422, resp.text

    async def test_refuses_the_whole_batch_when_one_id_is_another_owners(
        self, client, two_profiles
    ):
        a_project = await make_project("tags mine", owner=two_profiles["a"].owner_id())
        b_project = await make_project("tags theirs", owner=two_profiles["b"].owner_id())
        mine = await _fastq(a_project, "tag_mine.fastq")
        theirs = await _fastq(b_project, "tag_theirs.fastq")

        resp = await client.post(
            "/api/v1/objects/bulk-tags",
            json={"object_ids": [str(mine.id), str(theirs.id)], "add": ["x"]},
            headers=two_profiles["a_headers"],
        )

        assert resp.status_code == 404, resp.text
        await mine.sync()
        assert mine.tags == []

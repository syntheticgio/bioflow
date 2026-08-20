"""Report directories (qc_reports/, bam_stats/, vcf_stats/) are copied to the
recipient's object id on accept, per docs/superpowers/specs/
2026-08-05-profile-sharing-design.md, "Report directories do not follow the
object". The load-bearing test is that the copy survives the sender's own
deletion -- that is the case a shared_from-based fallback would get wrong.
"""

import pytest

from app.config import settings
from app.services import object_service, share_service
from tests.services.helpers_share import make_profile, ready_object

pytestmark = [pytest.mark.usefixtures("beanie_models"), pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    async def _skip(obj, **kwargs):
        return ""

    monkeypatch.setattr(object_service, "enqueue_ingest", _skip)


@pytest.fixture(autouse=True)
def _no_events(monkeypatch):
    from app.queue import queue

    async def _skip(*args, **kwargs):
        return None

    monkeypatch.setattr(queue, "publish_event", _skip)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_scratch():
    yield
    from tests.services.helpers_share import reclaim_scratch_files

    reclaim_scratch_files()


@pytest.fixture(autouse=True)
def _isolated_report_roots(tmp_path, monkeypatch):
    """Write share-report fixtures into tmp_path, not the live qc_reports_dir.

    `_write_report` and the assertions read `settings.qc_reports_dir` (a
    read-only property), while `share_service` copies through
    `object_service.copy_report_dirs`, which iterates the `_REPORT_ROOTS`
    tuple captured at import time. Redirect both, the way
    test_object_deletion's `private_report_roots` does, so a run leaves
    nothing under /data/qc_reports -- the `fastqc_report.html` filename the
    real handler never produces has already cost one #624 investigation.
    """
    qc_dir = tmp_path / "qc_reports"
    monkeypatch.setattr(type(settings), "qc_reports_dir", property(lambda _s: qc_dir))
    monkeypatch.setattr(object_service, "_REPORT_ROOTS", (qc_dir,))



def _write_report(object_id) -> None:
    report_dir = settings.qc_reports_dir / str(object_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "fastqc_report.html").write_bytes(b"<html>report</html>")


async def _offer_and_accept(*, sender: str, recipient: str, obj):
    share = await share_service.offer_share(owner=sender, object_id=obj.id, to_profile_id=recipient)
    return await share_service.accept_share(owner=recipient, share_id=share.id)


async def test_report_directory_is_copied_to_the_recipients_object_id():
    sender = await make_profile("share-report-copy-sender")
    recipient = await make_profile("share-report-copy-recipient")
    obj = await ready_object(owner=sender)
    _write_report(obj.id)

    copy = await _offer_and_accept(sender=sender, recipient=recipient, obj=obj)

    dst_report = settings.qc_reports_dir / str(copy.id) / "fastqc_report.html"
    assert dst_report.exists()
    assert dst_report.read_bytes() == b"<html>report</html>"


async def test_recipients_report_survives_the_senders_deletion():
    sender = await make_profile("share-report-survive-sender")
    recipient = await make_profile("share-report-survive-recipient")
    obj = await ready_object(owner=sender)
    _write_report(obj.id)

    copy = await _offer_and_accept(sender=sender, recipient=recipient, obj=obj)

    await object_service.delete_object(obj.id, owner=sender)

    dst_report = settings.qc_reports_dir / str(copy.id) / "fastqc_report.html"
    assert dst_report.exists()
    assert dst_report.read_bytes() == b"<html>report</html>"

    src_report_dir = settings.qc_reports_dir / str(obj.id)
    assert not src_report_dir.exists()


async def test_accepting_with_no_report_directory_creates_nothing():
    sender = await make_profile("share-report-none-sender")
    recipient = await make_profile("share-report-none-recipient")
    obj = await ready_object(owner=sender)

    copy = await _offer_and_accept(sender=sender, recipient=recipient, obj=obj)

    dst_dir = settings.qc_reports_dir / str(copy.id)
    assert not dst_dir.exists()


async def test_a_copy_failure_does_not_fail_the_accept(monkeypatch):
    sender = await make_profile("share-report-fail-sender")
    recipient = await make_profile("share-report-fail-recipient")
    obj = await ready_object(owner=sender)
    _write_report(obj.id)

    def _boom(*args, **kwargs):
        raise OSError("disk exploded")

    monkeypatch.setattr(object_service.shutil, "copytree", _boom)

    copy = await _offer_and_accept(sender=sender, recipient=recipient, obj=obj)

    assert copy is not None
    from app.models import ObjectStatus

    assert copy.status is ObjectStatus.READY

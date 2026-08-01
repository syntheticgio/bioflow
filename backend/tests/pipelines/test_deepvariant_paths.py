"""Container-to-host path translation for sibling containers.

A container started through the host's Docker daemon gets its mounts from the
host filesystem, not from the worker's. Passing the worker's own /data path
mounts an empty directory that happens to exist -- so DeepVariant fails "file
not found" on a BAM that is plainly there. These tests pin the translation and,
more importantly, that an untranslatable path raises rather than being passed
through.
"""

import pytest

from app.errors import PermanentError
from app.pipelines import variant_runner


class TestHostPathFor:
    def test_translates_a_path_under_the_storage_root(self):
        assert variant_runner.host_path_for(
            "/data/objects/ab/abcdef.bam",
            container_root="/data",
            host_root="/Volumes/Drive/Bio",
        ) == "/Volumes/Drive/Bio/objects/ab/abcdef.bam"

    def test_translates_the_root_itself(self):
        assert variant_runner.host_path_for(
            "/data", container_root="/data", host_root="/Volumes/Drive/Bio"
        ) == "/Volumes/Drive/Bio"

    def test_accepts_a_path_object(self):
        from pathlib import Path

        assert variant_runner.host_path_for(
            Path("/data/x.bam"),
            container_root="/data",
            host_root="/Volumes/Drive/Bio",
        ) == "/Volumes/Drive/Bio/x.bam"

    def test_a_path_outside_the_root_raises(self):
        """The case that must never silently succeed. A /tmp path would mount
        an empty directory and produce a confusing 'file not found' on a file
        that exists."""
        with pytest.raises(PermanentError) as e:
            variant_runner.host_path_for(
                "/tmp/scratch.bam",
                container_root="/data",
                host_root="/Volumes/Drive/Bio",
            )
        assert "/tmp/scratch.bam" in str(e.value)

    def test_a_prefix_lookalike_is_not_translated(self):
        """`/database/x` starts with the characters of `/data` but is not under
        it. String-prefix matching would translate it and mount nothing."""
        with pytest.raises(PermanentError):
            variant_runner.host_path_for(
                "/database/x.bam",
                container_root="/data",
                host_root="/Volumes/Drive/Bio",
            )

    def test_missing_host_root_raises_with_a_fixable_message(self):
        """An unset BIOINFO_HOME_HOST must name the variable, since the fix is
        a compose edit and nothing else will hint at it."""
        with pytest.raises(PermanentError) as e:
            variant_runner.host_path_for(
                "/data/x.bam", container_root="/data", host_root=""
            )
        assert "BIOINFO_HOME_HOST" in str(e.value)

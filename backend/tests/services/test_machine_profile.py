"""The host fingerprint stamped on every computation record."""

from app.services import machine_profile


class TestCapture:
    def test_reports_the_fields_a_record_needs(self):
        profile = machine_profile.capture()
        for field in (
            "cpu_model",
            "physical_cores",
            "logical_cores",
            "total_ram_bytes",
            "cgroup_cpu_budget",
            "cgroup_mem_limit",
            "platform",
            "machine_id",
        ):
            assert field in profile

    def test_core_counts_are_plausible(self):
        profile = machine_profile.capture()
        assert profile["logical_cores"] >= 1
        assert profile["total_ram_bytes"] > 0

    def test_machine_id_is_stable_across_calls(self):
        """Segmenting an aggregated corpus by hardware requires the same
        machine to hash the same way every time."""
        assert machine_profile.capture()["machine_id"] == machine_profile.capture()["machine_id"]

    def test_machine_id_is_not_a_hostname(self):
        """It must identify a machine to the model without identifying it to a
        person -- these records are meant to be uploadable."""
        import socket

        machine_id = machine_profile.capture()["machine_id"]
        assert socket.gethostname() not in machine_id
        assert len(machine_id) == 16
        int(machine_id, 16)  # raises unless it is pure hex


class TestCaching:
    def test_probes_once_and_reuses(self, monkeypatch):
        calls = []
        real = machine_profile._probe

        def counting_probe():
            calls.append(1)
            return real()

        machine_profile.reset_cache()
        monkeypatch.setattr(machine_profile, "_probe", counting_probe)
        machine_profile.capture()
        machine_profile.capture()
        assert len(calls) == 1
        machine_profile.reset_cache()


class TestCgroupBudgets:
    def test_budgets_come_from_the_governor_helpers(self, monkeypatch):
        """Inside Docker the cgroup limit is what actually binds, and psutil
        reports the VM's resources instead. Recording the wrong one would
        poison the memory model."""
        monkeypatch.setattr(
            "app.queue.governor._read_cgroup_cpu", lambda: 4.0
        )
        monkeypatch.setattr(
            "app.queue.governor._read_cgroup_mem", lambda: 8 * 1024**3
        )
        machine_profile.reset_cache()
        profile = machine_profile.capture()
        assert profile["cgroup_cpu_budget"] == 4.0
        assert profile["cgroup_mem_limit"] == 8 * 1024**3
        machine_profile.reset_cache()

    def test_unlimited_cgroup_reports_none_not_a_guess(self, monkeypatch):
        monkeypatch.setattr("app.queue.governor._read_cgroup_cpu", lambda: None)
        monkeypatch.setattr("app.queue.governor._read_cgroup_mem", lambda: None)
        machine_profile.reset_cache()
        profile = machine_profile.capture()
        assert profile["cgroup_cpu_budget"] is None
        assert profile["cgroup_mem_limit"] is None
        machine_profile.reset_cache()

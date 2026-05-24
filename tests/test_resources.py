from autocode.resources import format_job_resource, job_resource_for_pid, parse_ps_output


def test_parse_ps_output_and_job_resource_tree(monkeypatch):
    rows = parse_ps_output(
        """
          10     1   1.5  1024 /bin/zsh worker
          11    10   2.5  2048 python child
          12    11   0.5  1024 node grandchild
          99     1  50.0  9999 unrelated
        """
    )

    monkeypatch.setattr("autocode.resources.process_table", lambda: rows)

    resource = job_resource_for_pid(10)

    assert resource.process_count == 3
    assert resource.cpu_percent == 4.5
    assert resource.rss_mb == 4.0
    assert "python child" in resource.sample
    assert format_job_resource(resource) == "cpu~4%, ram~4MB, procs=3"

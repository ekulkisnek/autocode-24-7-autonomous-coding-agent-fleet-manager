from pathlib import Path

from autocode.dashboard import _job_working_text, model_info, render_dashboard
from autocode.models import Chat
from autocode.store import Store
from autocode.util import json_dumps, now_iso


def test_dashboard_renders_running_job_model_and_usage(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="cursor:cursor.cli:agent-123",
        provider="cursor",
        source="cursor.cli",
        provider_chat_id="agent-123",
        alias="redwallet-cursor-helper",
        title="RedWallet security audit",
        cwd="/tmp/redwallet",
        updated_at=now_iso(),
        latest_text="Audit wallet persistence.",
        continuation="cursor-agent --resume",
        metadata={"model": "composer-2.5", "active": True},
    )
    store.upsert_chat(chat, coding_score=3, state="active", objective="Make RedWallet safer and cleaner.")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at,evidence_status)
            values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-test",
                chat.id,
                "cursor",
                "running",
                0,
                "/tmp/redwallet",
                json_dumps(["cursor-agent", "--resume", "agent-123", "--model", "composer-2.5", "continue"]),
                "Continue the RedWallet audit.",
                str(tmp_path / "stdout.txt"),
                str(tmp_path / "stderr.txt"),
                now_iso(),
                now_iso(),
                "running_working",
            ),
        )
    (tmp_path / "stdout.txt").write_text("Inspecting auth and persistence flows.", encoding="utf-8")
    (tmp_path / "stderr.txt").write_text("", encoding="utf-8")

    text = render_dashboard(store, width=120, limit=5, refresh_jobs=False)

    assert "AutoCode Dashboard" in text
    assert "disk=" in text
    assert "session prompts:" in text
    assert "Driving Now" in text
    assert "prompts: session=" in text
    assert "composer-2.5" in text
    assert "redwallet-cursor-helper" in text
    assert "Provider Usage / Health" in text
    assert "cursor" in text
    assert "remaining" in text
    assert "not exposed" in text


def test_model_info_reads_effort_and_fast_model(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-model",
                "grok:grok.sqlite:1",
                "grok",
                "running",
                0,
                "/tmp",
                json_dumps(["grok", "--model", "grok-build-fast", "--effort", "high"]),
                "go",
                str(tmp_path / "out.txt"),
                str(tmp_path / "err.txt"),
                now_iso(),
                now_iso(),
            ),
        )
    row = store.row("select * from jobs where id='job-model'")

    info = model_info(row)

    assert info.model == "grok-build-fast"
    assert info.effort == "high"
    assert info.speed == "fast"


def test_job_working_text_prefers_status_over_shell_trace(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
+ export PATH="$JAVA_HOME/bin:$PATH"
+ exec "$@"
diff --git a/package.json b/package.json
index 31bce59ff..24eb049f1 100644
--- a/package.json
+++ b/package.json
JAVA_HOME=/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
ANDROID_HOME=/Volumes/T705/code/android-commandlinetools
OpenJDK 64-Bit Server VM Homebrew
FLEET_MILESTONE_COMPLETE
Operating rule: work continuously and autonomously in this exact Codex chat.
Fixed Android build env wrapper and verified Java 17 plus Gradle can run.
Next action: run the Android BitAssets Detox build using the wrapper.
tokens used
""",
        encoding="utf-8",
    )
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-readable",
                "codex:codex.rollout:redwallet",
                "codex",
                "running",
                0,
                "/tmp",
                "[]",
                "Continue RedWallet.",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )
    row = store.row("select * from jobs where id='job-readable'")

    text = _job_working_text(row, 500)

    assert "Fixed Android build env wrapper" in text
    assert "Next action" in text
    assert "export PATH" not in text
    assert "diff --git" not in text


def test_job_working_text_summarizes_gradle_progress(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
> Task :app:mergeDebugJavaResource UP-TO-DATE
> Task :app:mergeProjectDexDebug
> Task :react-native-worklets:configureCMakeDebug
""",
        encoding="utf-8",
    )
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-gradle",
                "codex:codex.rollout:redwallet",
                "codex",
                "running",
                0,
                "/tmp",
                "[]",
                "Continue RedWallet.",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )
    row = store.row("select * from jobs where id='job-gradle'")

    text = _job_working_text(row, 500)

    assert text == "Android/Gradle build running: latest task :react-native-worklets:configureCMakeDebug"


def test_job_working_text_does_not_show_operating_rules_as_work(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-waiting",
                "codex:codex.rollout:redwallet",
                "codex",
                "running",
                0,
                "/tmp",
                "[]",
                "Operating rule: work continuously and autonomously.\nCurrent known next step: run the Android build and inspect failures.",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )
    row = store.row("select * from jobs where id='job-waiting'")

    text = _job_working_text(row, 500)

    assert text == "Waiting for first agent output; assigned: run the Android build and inspect failures."
    assert "Operating rule" not in text

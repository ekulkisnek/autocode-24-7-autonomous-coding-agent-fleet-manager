from pathlib import Path

from autocode.dashboard import _job_working_text, _objective_summary, model_info, render_dashboard
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
    assert "quota:" in text
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
new file mode 100644
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
    assert "new file mode" not in text


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


def test_job_working_text_does_not_show_yolo_prompt_as_work(tmp_path: Path):
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
                "job-yolo-prompt",
                "codex:codex.rollout:redwallet",
                "codex",
                "running",
                0,
                "/tmp",
                "[]",
                "AutoCode is driving this project in Maximum YOLO mode.",
                str(stdout),
                str(stderr),
                now_iso(),
                now_iso(),
            ),
        )
    row = store.row("select * from jobs where id='job-yolo-prompt'")

    text = _job_working_text(row, 500)

    assert text == "Waiting for first agent output"
    assert "Maximum YOLO" not in text


def test_job_working_text_does_not_show_numbered_goal_outline_as_work(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
6. Live full-system proof:
7. Environment/CI closure:
Current evidence: Android SDK root repair is in progress.
Next action: rerun Android Detox after SDK path correction.
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
                "job-numbered-outline",
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
    row = store.row("select * from jobs where id='job-numbered-outline'")

    text = _job_working_text(row, 500)

    assert "Current evidence" in text
    assert "Next action" in text
    assert "Live full-system proof" not in text
    assert "Environment/CI closure" not in text


def test_job_working_text_filters_package_json_script_fragments(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
"e2e:bitassets:ios": "BITASSETS_E2E=1 detox test -c ios.debug tests/e2e/bitassets.spec.js --loglevel info --reuse",
"android.release": { } } }
{ }
"BITASSETS_E2E=1 detox test -c android.debug tests/e2e/bitassets.spec.js --loglevel info --reuse --no-build",
Current background build: Android build is still running.
Next action: inspect the Android build result.
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
                "job-package-json",
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
    row = store.row("select * from jobs where id='job-package-json'")

    text = _job_working_text(row, 500)

    assert "Current background build" in text
    assert "Next action" in text
    assert "e2e:bitassets:ios" not in text
    assert "android.release" not in text
    assert "android.debug" not in text


def test_job_working_text_filters_repeated_handoff_boilerplate(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text(
        """
handoff here:
CODEX_HANDOFF_NATIVE_SIGNER.md
I also linked it from the top of:
AGENT_COORDINATION.md
and logged the context/handoff checkpoint in:
LOCAL_DEVELOPMENT_NOTES.md
the handoff includes the short current checkpoint and next commands.
short current checkpoint: native signer still needs Detox verification.
Current build: iOS Detox validation is running in tmux.
Next action: inspect the iOS build log and fix the first concrete failure.
""",
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-handoff-noise",
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
    row = store.row("select * from jobs where id='job-handoff-noise'")

    text = _job_working_text(row, 500)

    assert "iOS Detox validation is running" in text
    assert "Next action" in text
    assert "handoff here" not in text
    assert "CODEX_HANDOFF_NATIVE_SIGNER" not in text
    assert "AGENT_COORDINATION" not in text
    assert "LOCAL_DEVELOPMENT_NOTES" not in text
    assert "the handoff includes" not in text
    assert "short current checkpoint" not in text


def test_job_working_text_filters_process_list_and_status_command_noise(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text(
        """
3558 00:01 /bin/zsh -lc ps -axo pid,etime,command | rg 'autocode|codex'
3971 00:00 /opt/homebrew/bin/rg CODEX_HANDOFF_NATIVE_SIGNER.md /tmp/redwallet
tmux capture-pane -pt redwallet-ios:0 -S -200
status commands: ps -axo pid,etime,command
rg "handoff here:" CODEX_HANDOFF_NATIVE_SIGNER.md AGENT_COORDINATION.md
Current evidence: Android native signer build passed Java compilation.
Verified BitAssets storage migration no longer crashes on empty wallet state.
Next action: rerun Detox BitAssets smoke test and capture the first failing screen.
""",
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-process-noise",
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
    row = store.row("select * from jobs where id='job-process-noise'")

    text = _job_working_text(row, 500)

    assert "Current evidence" in text
    assert "Verified BitAssets" in text
    assert "Next action" in text
    assert "ps -axo" not in text
    assert "tmux capture-pane" not in text
    assert "rg " not in text


def test_job_working_text_trims_milestone_evidence_command_tail(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
FLEET_MILESTONE_COMPLETE iOS build gate passed and iOS BitAssets E2E gate is now running. Completed evidence: - `npx detox test -c ios.debug tests/e2e/bitassets.spec.js`
exec
/bin/zsh -lc "rg -n 'DETOX_EXIT|PASS|FAIL' /Volumes/T705/redwallet-logs/redwallet-bitassets-ios-e2e-rerun.log || true"
succeeded in 0ms:
Next action: monitor the E2E run to completion and capture the first actionable failure.
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
                "job-milestone-command-tail",
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
    row = store.row("select * from jobs where id='job-milestone-command-tail'")

    text = _job_working_text(row, 500)

    assert "iOS build gate passed" in text
    assert "Next action" in text
    assert "FLEET_MILESTONE_COMPLETE" not in text
    assert "Completed evidence" not in text
    assert "npx detox" not in text
    assert "/bin/zsh" not in text


def test_job_working_text_drops_partial_first_line_from_truncated_log(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        ("x" * 500)
        + "d `adb` and `emulator`. - Validation: - `bash -n scripts/with-android-build-env.sh`\n"
        + "Current evidence: Android E2E failed on broken AVD system path.\n"
        + "Next action: fix ANDROID_SDK_ROOT to point at a valid SDK root and rerun Detox.\n",
        encoding="utf-8",
    )
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-truncated-log",
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
    row = store.row("select * from jobs where id='job-truncated-log'")

    text = _job_working_text(row, 100)

    assert "Current evidence" in text
    assert "Next action" in text
    assert "d `adb`" not in text
    assert "Validation:" not in text


def test_job_working_text_filters_sdk_probe_command_continuations(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
Pixel_API_29_AOSP "$HOME/Library/Android/sdk" \\
  /Volumes/T705/code/android-commandlinetools \\
  /opt/homebrew/share/android-commandlinetools
Current evidence: Android E2E failed because the AVD SDK root is invalid.
Next action: point ANDROID_SDK_ROOT at a valid SDK root and rerun Detox.
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
                "job-sdk-probe",
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
    row = store.row("select * from jobs where id='job-sdk-probe'")

    text = _job_working_text(row, 500)

    assert "Current evidence" in text
    assert "Next action" in text
    assert "Pixel_API_29_AOSP" not in text
    assert "commandlinetools" not in text


def test_job_working_text_filters_line_numbered_rg_log_matches(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text(
        """
6:17:49:45.775 detox[7613] B jest --config tests/e2e/jest.config.js --no-build tests/e2e/bitassets.spec.js
Current evidence: Android Detox is running after SDK root correction.
Next action: wait for DETOX_EXIT and inspect the first actionable failure.
""",
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-rg-log-match",
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
    row = store.row("select * from jobs where id='job-rg-log-match'")

    text = _job_working_text(row, 500)

    assert "Current evidence" in text
    assert "Next action" in text
    assert "jest --config" not in text


def test_job_working_text_filters_raw_detox_timestamp_chatter(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text(
        """
(Use `node --trace-deprecation ...` to show where the warning was created)
17:50:06.902 detox[7618] i bitassets.spec.js is assigned to emulator-5554 (Pixel_API_29_AOSP)
Current evidence: Android Detox reached the BitAssets spec.
Next action: monitor for DETOX_EXIT and capture the first real failure.
""",
        encoding="utf-8",
    )
    stderr.write_text("", encoding="utf-8")
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,created_at,updated_at)
            values(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-detox-chatter",
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
    row = store.row("select * from jobs where id='job-detox-chatter'")

    text = _job_working_text(row, 500)

    assert "Current evidence" in text
    assert "Next action" in text
    assert "trace-deprecation" not in text
    assert "detox[7618]" not in text


def test_objective_summary_strips_completion_boilerplate():
    raw = (
        "Hard completion definition: ship the Android build and pass Detox. "
        "Operating rule: work continuously until done. "
        "Make RedWallet safer by fixing auth persistence and cleaning up wallet storage."
    )

    summary = _objective_summary(raw)

    assert "Hard completion definition" not in summary
    assert "Operating rule" not in summary
    assert "Make RedWallet safer" in summary


def test_recent_section_shows_done_summary_not_raw_bytes(tmp_path: Path):
    store = Store(tmp_path / "autocode.sqlite")
    chat = Chat(
        id="codex:codex.rollout:done-chat",
        provider="codex",
        source="codex.rollout",
        provider_chat_id="done-chat",
        alias="redwallet-build",
        title="RedWallet build",
        cwd="/tmp/redwallet",
        updated_at=now_iso(),
        latest_text="Build Android app.",
        continuation="codex continue",
        metadata={},
    )
    store.upsert_chat(chat, coding_score=2, state="active", objective="Ship Android build.")
    stdout = tmp_path / "stdout.txt"
    stderr = tmp_path / "stderr.txt"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text(
        """
Fixed Android build env wrapper and verified Java 17 plus Gradle can run.
Next action: run the Android BitAssets Detox build using the wrapper.
""",
        encoding="utf-8",
    )
    finished_at = now_iso()
    with store.connect() as con:
        con.execute(
            """
            insert into jobs(
                id,chat_id,provider,status,pid,cwd,cmd_json,prompt,stdout_path,stderr_path,
                created_at,updated_at,completed_at,evidence_status,evidence_reason
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "job-done",
                chat.id,
                "codex",
                "completed",
                0,
                "/tmp/redwallet",
                "[]",
                "Continue RedWallet.",
                str(stdout),
                str(stderr),
                finished_at,
                finished_at,
                finished_at,
                "worked",
                "process exited; stdout_bytes=0; stderr_bytes=512",
            ),
        )

    text = render_dashboard(store, width=120, limit=5, refresh_jobs=False)

    assert "Recent Evidence" in text
    assert "done: Fixed Android build env wrapper" in text
    assert "stdout_bytes=" not in text
    assert "stderr 512B" in text

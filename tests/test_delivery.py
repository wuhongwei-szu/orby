"""Offline regressions for delivery and reminder creation; no gateway calls."""
import contextlib
import importlib.util
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load_tool(name, common, **dependencies):
    spec = importlib.util.spec_from_file_location("delivery_test_" + name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"common": common, **dependencies}):
        spec.loader.exec_module(module)
    return module


class ImageCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=TOOLS.parent, prefix="synthetic-delivery-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.image = self.root / "fixture 图片.png"
        self.image.write_bytes(b"offline image payload")
        self.config_path = self.root / "bot.json"
        self.config = {
            "work_dir": str(self.root), "sticker_dir": str(self.root), "cc_data_dir": str(self.root),
            "cc_bin": str(self.root / "offline-cc.exe"), "cc_project": "fixture",
            "cc_config_file": str(self.root / "cc.toml"), "session_file": str(self.root / "sessions.json"),
            "master_account_id": "master-bot", "xi_account_id": "xi-bot",
            "weixin_api_url": "https://offline.invalid/ilink/bot/sendmessage",
        }
        (self.root / "cc.toml").write_text(
            "[[projects]]\nname='fixture'\n" + "".join(
                "[[projects.platforms]]\ntype='weixin'\n[projects.platforms.options]\n"
                f"account_id='{role}-bot'\ntoken='fixture-only'\n" for role in ("master", "xi")
            ), encoding="utf-8",
        )
        (self.root / "sessions.json").write_text(json.dumps({
            "user_sessions": {"weixin:dm:master-peer": "s1", "weixin:dm:xi-peer": "s2"},
        }), encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps({"stickers": [{
            "id": "fish", "category": "happy", "style": "cat", "file": self.image.name,
        }]}), encoding="utf-8")
        self.cc_run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "Message sent successfully.", ""))
        self.http_open = mock.Mock(side_effect=AssertionError("Unexpected network request"))

    def cli(self, script, source, *args, session=""):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        path = str(TOOLS / f"{script}.py")
        env = {"BOT_CONFIG": str(self.config_path), "CC_SESSION_KEY": session,
               "CC_PLATFORM_ACCOUNT": f"{source}-bot" if source else ""}
        stdout, stderr = io.StringIO(), io.StringIO()
        entry_modules = ("common", "send_msg", "send_xi")
        previous = {name: sys.modules.pop(name) for name in entry_modules if name in sys.modules}
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", [path, *args]), \
                    mock.patch.object(sys, "path", [str(TOOLS), *sys.path]), \
                    mock.patch.object(subprocess, "run", self.cc_run), \
                    mock.patch.object(urllib.request.OpenerDirector, "open", self.http_open), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(path, run_name="__main__")
        finally:
            for module in entry_modules:
                sys.modules.pop(module, None)
            sys.modules.update(previous)
        status = exited.exception.code
        if isinstance(status, str):
            return 1, stdout.getvalue() + stderr.getvalue() + status
        return status, stdout.getvalue() + stderr.getvalue()

    def assert_cc_image(self, target):
        self.cc_run.assert_called_once()
        self.http_open.assert_not_called()
        self.assertEqual(self.cc_run.call_args.args[0], [
            self.config["cc_bin"], "send", "-p", "fixture", "--session", f"weixin:dm:{target}-peer",
            "--image", str(self.image), "--account", f"{target}-bot", "--data-dir", str(self.root),
        ])

    def prepare_ilink(self, receipt=b'{"ret":0}'):
        context_path = self.root / "weixin/fixture/xi-bot/context_tokens.json"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text('{"xi-peer":"fixture-context"}', encoding="utf-8")

        def response(body=b"", headers=None):
            return contextlib.nullcontext(types.SimpleNamespace(
                status=200, headers=headers or {}, read=lambda size=-1: body,
            ))

        self.http_open.side_effect = [
            response(b'{"upload_full_url":"https://offline.invalid/upload"}'),
            response(headers={"x-encrypted-param": "fixture-upload"}),
            response(receipt) if isinstance(receipt, bytes) else receipt,
        ]

    def assert_ilink_image(self):
        self.cc_run.assert_not_called()
        requests = [call.args[0] for call in self.http_open.call_args_list]
        self.assertEqual([request.full_url for request in requests], [
            "https://offline.invalid/ilink/bot/getuploadurl", "https://offline.invalid/upload",
            self.config["weixin_api_url"],
        ])
        upload = json.loads(requests[0].data)
        self.assertEqual((upload["to_user_id"], upload["media_type"], upload["rawsize"]),
                         ("xi-peer", 1, self.image.stat().st_size))
        message = json.loads(requests[2].data)["msg"]
        self.assertEqual(message["to_user_id"], "xi-peer")
        self.assertEqual(len(message["item_list"]), 1)
        item = message["item_list"][0]
        self.assertEqual((item["type"], set(item)), (2, {"type", "image_item"}))

    def assert_no_send(self):
        self.cc_run.assert_not_called()
        self.http_open.assert_not_called()

    def assert_no_receipt_record(self):
        for name in ("outbound_state.json", "sticker_usage.json", "raw"):
            self.assertFalse((self.root / "memory" / name).exists(), name)

    def test_xi_can_relay_selected_sticker_to_master_without_text(self):
        status, receipt = self.cli("send_sticker", "xi", "master", "--relay", "--id", "fish")
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("master")
        self.assertIn("接口已接受", receipt)
        self.assertIn("收件待核实", receipt)

    def test_relay_generated_image_is_one_image_and_no_text(self):
        status, receipt = self.cli("send_msg", "xi", "master", "--relay", "--image", str(self.image))
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("master")
        self.assertIn("接口已接受", receipt)
        self.assertIn("收件待核实", receipt)

    def test_sticker_relay_uses_recipient_account_with_stale_session_env(self):
        status, receipt = self.cli("send_sticker", "xi", "master", "--relay", "--id", "fish",
                                   session="weixin:dm:master-peer")
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("master")

    def test_master_can_relay_matching_sticker_to_xi_without_text(self):
        self.prepare_ilink()
        status, receipt = self.cli("send_sticker", None, "xi", "--relay", "--category", "happy",
                                   session="fixture:weixin:dm:master-peer")
        self.assertEqual(status, 0, receipt)
        self.assert_ilink_image()
        self.assertIn("iLink 接口已接受", receipt)
        self.assertIn("收件待核实", receipt)

    def test_master_can_relay_generated_image_to_xi_without_text(self):
        self.prepare_ilink()
        status, receipt = self.cli("send_msg", "master", "xi", "--relay", "--image", str(self.image))
        self.assertEqual(status, 0, receipt)
        self.assert_ilink_image()
        self.assertIn("iLink 接口已接受", receipt)
        self.assertIn("收件待核实", receipt)

    def test_same_session_sticker_remains_available_without_relay(self):
        status, receipt = self.cli("send_sticker", "xi", "xi", "--category", "happy")
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("xi")

    def test_cross_recipient_requires_relay_and_relay_requires_distinct_known_source(self):
        for script, payload in (("send_sticker", ("--id", "fish")),
                                ("send_msg", ("--image", str(self.image)))):
            for source, flags in (("xi", ()), (None, ("--relay",)),
                                  ("unknown", ("--relay",)), ("master", ("--relay",))):
                with self.subTest(script=script, source=source, flags=flags):
                    status, receipt = self.cli(script, source, "master", *flags, *payload)
                    self.assertEqual(status, 1, receipt)
        self.assert_no_send()
        self.assert_no_receipt_record()

    def test_relay_sticker_with_specific_theme_never_sends_unrelated_choice(self):
        status, receipt = self.cli("send_sticker", "xi", "master", "--relay", "--category", "happy",
                                   "--prefer", "fixture-missing-theme")
        self.assertEqual(status, 3, receipt)
        self.assert_no_send()
        self.assert_no_receipt_record()

    def test_generated_image_in_current_conversation_uses_normal_route(self):
        status, receipt = self.cli("send_msg", "master", "master", "--image", str(self.image))
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("master")
        self.assertIn("收件待核实", receipt)

    def test_standalone_explicit_local_sticker_still_works(self):
        status, receipt = self.cli("send_sticker", None, "master", "--id", "fish")
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("master")

    def test_image_acceptance_stays_true_if_local_archive_fails(self):
        (self.root / "memory").mkdir()
        (self.root / "memory/raw").write_text("not a directory", encoding="utf-8")
        status, receipt = self.cli("send_msg", "xi", "master", "--relay", "--image", str(self.image))
        self.assertEqual(status, 0, receipt)
        self.assertIn("接口已接受", receipt)
        self.assertIn("记录", receipt)
        self.assert_cc_image("master")

    def test_sticker_relay_conflicts_with_proactive(self):
        status, receipt = self.cli("send_sticker", "xi", "master", "--relay", "--proactive", "--id", "fish")
        self.assertEqual(status, 2, receipt)
        self.assert_no_send()

    def test_sticker_rejects_an_abbreviated_relay_flag(self):
        status, receipt = self.cli("send_sticker", "xi", "master", "--rel", "--id", "fish")
        self.assertEqual(status, 2, receipt)
        self.assert_no_send()

    def test_invalid_image_and_mixed_payload_never_reach_transport(self):
        missing = self.root / "missing.png"
        empty = self.root / "empty.png"
        empty.touch()
        for args in (("--image", str(missing)), ("--image", str(self.root)), ("--image", str(empty)),
                     ("--image",), ("--image", str(self.image), "不能附带文字"),
                     ("--stdin", "--image", str(self.image)), ("--image=",)):
            with self.subTest(args=args):
                status, receipt = self.cli("send_msg", "xi", "master", "--relay", *args)
                self.assertEqual(status, 1, receipt)
        self.image.unlink()
        status, receipt = self.cli("send_sticker", "xi", "master", "--relay", "--id", "fish")
        self.assertEqual(status, 1, receipt)
        self.assert_no_send()
        self.assert_no_receipt_record()

    def test_missing_recipient_configuration_never_reaches_transport(self):
        self.config["cc_bin"] = ""
        status, receipt = self.cli("send_msg", "xi", "master", "--relay", "--image", str(self.image))
        self.assertEqual(status, 1, receipt)
        self.assertIn("缺少配置", receipt)
        self.config["xi_session_id"] = "missing"
        status, receipt = self.cli("send_sticker", "master", "xi", "--relay", "--id", "fish")
        self.assertEqual(status, 2, receipt)
        self.assertIn("未提交", receipt)
        self.assert_no_send()

    def test_sticker_relay_keeps_recipient_quota_cooldown_and_accounting(self):
        self.config["sticker_cooldown_seconds"] = 600
        args = ("send_sticker", "xi", "master", "--relay", "--id", "fish")
        status, receipt = self.cli(*args)
        self.assertEqual(status, 0, receipt)
        self.assert_cc_image("master")
        self.cc_run.reset_mock()
        status, receipt = self.cli(*args)
        self.assertEqual(status, 3, receipt)
        self.assertIn("刚发过表情", receipt)
        self.config["sticker_daily_limit"] = 1
        status, receipt = self.cli(*args, "--force")
        self.assertEqual(status, 3, receipt)
        self.assertIn("表情额度用完", receipt)
        self.assert_no_send()
        usage = json.loads((self.root / "memory/sticker_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(set(usage), {"master"})
        self.assertEqual([(item["id"], item["proactive"]) for item in usage["master"]["sent"]],
                         [("fish", False)])
        outbound = json.loads((self.root / "memory/outbound_state.json").read_text(encoding="utf-8"))
        self.assertEqual(set(outbound), {"master"})
        self.assertEqual(next(iter(outbound["master"].values()))["replies"], 1)

    def test_cc_failure_or_timeout_never_resends_or_records(self):
        for script, payload in (("send_sticker", ("--id", "fish")),
                                ("send_msg", ("--image", str(self.image)))):
            for failure in (subprocess.CompletedProcess([], 1, "", "opaque fixture response"),
                            subprocess.CompletedProcess([], 0, "", ""),
                            subprocess.TimeoutExpired("fixture command", 60)):
                with self.subTest(script=script, failure=failure):
                    self.cc_run.reset_mock(side_effect=True)
                    self.cc_run.side_effect = [failure]
                    status, receipt = self.cli(script, "xi", "master", "--relay", *payload)
                    self.assertEqual(status, 1, receipt)
                    self.assertIn("结果未确认", receipt)
                    self.assertNotIn("接口已接受", receipt)
                    self.assert_cc_image("master")
                    self.assert_no_receipt_record()

    def test_ilink_rejection_invalid_reply_or_timeout_never_resends_or_records(self):
        for reply, expected in ((b'{"errcode":-2}', "接口拒绝"), (b"invalid response", "结果未确认"),
                                (TimeoutError("fixture timeout"), "结果未确认")):
            with self.subTest(expected=expected, reply=type(reply).__name__):
                self.http_open.reset_mock()
                self.prepare_ilink(reply)
                status, receipt = self.cli("send_msg", "master", "xi", "--relay", "--image", str(self.image))
                self.assertEqual(status, 1, receipt)
                self.assertIn(expected, receipt)
                self.assertNotIn("接口已接受", receipt)
                self.assert_ilink_image()
                self.assert_no_receipt_record()

    def test_text_receipt_preserves_exit_codes(self):
        status, receipt = self.cli("send_msg", "master", "master", "文字消息")
        self.assertEqual(status, 0, receipt)
        self.assertIn("接口已接受文字发送请求", receipt)
        self.assertIn("收件待核实", receipt)
        self.assertEqual(self.cc_run.call_args.args[0][6:8], ["-m", "文字消息"])
        self.cc_run.return_value = subprocess.CompletedProcess([], 1, "", "send budget exhausted")
        status, receipt = self.cli("send_msg", "master", "master", "文字消息")
        self.assertEqual(status, 2, receipt)
        self.assertNotIn("接口已接受", receipt)
        self.http_open.assert_not_called()


class DeliveryTests(unittest.TestCase):
    def test_sticker_rotation_before_style_with_explicit_and_singleton_requests(self):
        common = types.SimpleNamespace(STICKER_DIR=".", STICKER_COOLDOWN_SECONDS=30,
                                       load_json=lambda *args: {}, outbound_serialized=lambda fn: fn)
        tool = load_tool("send_sticker", common)
        fish = {"id": "fish", "category": "happy", "style": "silly"}
        star = {"id": "star", "category": "happy", "style": "generic"}
        sent = {"sent": [{"id": "fish", "at": tool._now().isoformat()}]}
        def pick(entries, sticker_id=None):
            return tool._select_candidate({"stickers": entries}, "happy", "silly",
                                          sticker_id, sent, "master")
        self.assertEqual(pick([fish, star]), star)
        self.assertEqual(pick([fish]), fish)
        self.assertEqual(pick([fish, star], "fish"), fish)

    def native_voice_tool(self, payload):
        common = types.SimpleNamespace(
            outbound_serialized=lambda function: function,
            assert_target=lambda target: None,
            record_outbound=mock.Mock(),
            WEIXIN_API_BASE="https://offline.invalid", WEIXIN_API_URL="https://offline.invalid/send",
        )
        helper = load_tool("send_xi", common)
        tool = load_tool("send_voice", common, send_xi=helper)
        tool._target = lambda target: ("offline-token", "offline-session", "offline-context")
        tool._peer_context = lambda *args: ("offline-peer", "offline-context")
        tool.Cipher = lambda *args: types.SimpleNamespace(encryptor=lambda: types.SimpleNamespace(
            update=lambda data: data, finalize=lambda: b""))
        tool._post_json = lambda url, body, token, stage: (
            200, {}, b'{"upload_full_url":"https://offline.invalid/upload"}'
            if stage == "getuploadurl" else payload,
        )
        response = types.SimpleNamespace(headers={"x-encrypted-param": "offline"}, read=lambda: b"")
        tool._opener = lambda: types.SimpleNamespace(
            open=lambda *args, **kwargs: contextlib.nullcontext(response))
        return tool

    def test_native_voice_does_not_accept_invalid_or_rejected_response(self):
        for payload in (b"invalid JSON", b"null", b"[]", b'{"ret":0,"errcode":-2}'):
            with self.subTest(payload=payload):
                tool = self.native_voice_tool(payload)
                with mock.patch("builtins.open", mock.mock_open(read_data=b"#!AMR\nvoice")), \
                        mock.patch.object(tool.os.path, "isfile", return_value=True):
                    with self.assertRaises(tool.VoiceError):
                        tool.send_voice("master", "offline.amr")
                tool.c.record_outbound.assert_not_called()

    def test_native_voice_accepted_response_records_once(self):
        tool = self.native_voice_tool(b'{"ret":0}')
        with mock.patch("builtins.open", mock.mock_open(read_data=b"#!AMR\nvoice")), \
                mock.patch.object(tool.os.path, "isfile", return_value=True):
            result = tool.send_voice("master", "offline.amr")
        self.assertEqual(result["outcome"], "accepted")
        tool.c.record_outbound.assert_called_once_with("master")

    def test_uncertain_tts_send_is_attempted_once(self):
        # The helper owns route selection and reports an uncertain cc exit.
        # Each invocation here represents a potentially delivered file.
        attempts = []

        def run_cc(*args, **kwargs):
            attempts.append("file")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        def send_file(path):
            run_cc(path)
            raise RuntimeError("delivery uncertain; do not retry")

        common = types.SimpleNamespace(
            EDGE_TTS_VOICE="test", XI_SESSION="test-session", PROJECT="test",
            current_target=lambda: "xi", run_cc=run_cc,
            outbound_serialized=lambda function: function,
        )
        tool = load_tool("send_tts", common, send_xi=types.SimpleNamespace(send_file=send_file))
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            tool._send_xi("offline.mp3")
        self.assertEqual(attempts, ["file"])

    def test_both_reminder_retry_creates_only_missing_owner(self):
        items = []
        attempts = []
        fail_partner = True

        def run_cc(args):
            owner = args[args.index("-s") + 1]
            attempts.append(owner)
            if owner == "partner-session" and fail_partner:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="offline failure")
            return types.SimpleNamespace(returncode=0, stdout="Timer created: timer-" + owner, stderr="")

        common = types.SimpleNamespace(
            MASTER_SESSION="owner-session", XI_SESSION="partner-session", PROJECT="test",
            WORK_DIR="C:/offline", LABELS={}, run_cc=run_cc,
            require=lambda value, name: None,
            REMINDERS_INDEX="offline-index.json",
        )
        class NotStarted(RuntimeError):
            pass

        def add_timer(args):
            result = run_cc(args)
            if result.returncode:
                raise NotStarted("offline executable unavailable")
            return result.stdout.split(":", 1)[1].strip(), result.stdout

        state = types.SimpleNamespace(add_timer=add_timer, NotStarted=NotStarted,
                                      StateError=RuntimeError, locked=lambda path: contextlib.nullcontext())
        tool = load_tool("add_reminder", common, care_timer_state=state)
        tool.load_index = lambda: items
        tool.save_index = lambda saved: None
        argv = ["add_reminder.py", "--owner", "both", "--date", "2099-11-10",
                "--time", "12:00", "--title", "offline reminder"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(), 1)
            fail_partner = False
            self.assertEqual(tool.main(), 0)
        self.assertEqual(attempts, ["owner-session", "partner-session", "partner-session"])
        self.assertEqual({item["owner"] for item in items}, {"master", "xi"})

    def test_uncertain_reminder_creation_is_not_retried(self):
        items = []
        attempts = []
        common = types.SimpleNamespace(
            MASTER_SESSION="owner-session", XI_SESSION="partner-session", PROJECT="test",
            WORK_DIR="C:/offline", LABELS={}, REMINDERS_INDEX="offline-index.json",
            require=lambda value, name: None,
        )

        class NotStarted(RuntimeError):
            pass

        def add_timer(args):
            attempts.append("timer")
            raise TimeoutError("response lost after possible creation")

        state = types.SimpleNamespace(add_timer=add_timer, NotStarted=NotStarted,
                                      StateError=RuntimeError, locked=lambda path: contextlib.nullcontext())
        tool = load_tool("add_reminder", common, care_timer_state=state)
        tool.load_index = lambda: items
        tool.save_index = lambda saved: None
        argv = ["add_reminder.py", "--owner", "master", "--date", "2099-11-10",
                "--time", "12:00", "--title", "offline reminder"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(), 1)
            self.assertEqual(tool.main(), 1)
        self.assertEqual(attempts, ["timer"])
        self.assertEqual(items[0]["status"], "uncertain")


if __name__ == "__main__":
    unittest.main(verbosity=2)

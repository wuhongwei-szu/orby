#!/usr/bin/env python3
"""Send one WeChat message as the companion and log it to raw memory.

Usage:
    python -X utf8 send_msg.py master "message"
    python -X utf8 send_msg.py xi "message"
    python -X utf8 send_msg.py xi --relay "message"   # explicit cross-bot relay
    python -X utf8 send_msg.py master|xi [--relay] --image "C:\\path\\image.png"

Use one call per chat bubble. End the agent turn with NO_REPLY.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as c


IMAGE_USAGE = '请使用 python -X utf8 send_msg.py master|xi [--relay] --image "图片路径"'


def assert_image_target(target, *, relay=False):
    """Use --relay only when the current message authorizes a cross-bot image."""
    if target not in ("master", "xi"):
        raise SystemExit("目标必须是 master 或 xi")
    source = c.current_target()
    if source not in ("master", "xi"):
        raise SystemExit("无法确认当前会话身份，图片未提交。")
    if relay and source == target:
        raise SystemExit("--relay 需要不同的来源和目标会话，图片未提交。")
    c.assert_target(target, relay=relay)


def _print_accepted(transport, kind):
    print(f"{transport} 接口已接受{kind}发送请求，收件待核实。")


def _image_unconfirmed(transport):
    print(f"{transport} 图片发送结果未确认，收件待核实。请勿自动重发。")
    return 1


def _send_image_once(target, path, *, relay=False):
    """Dispatch an authorized image using the recipient's existing channel."""
    path = os.path.abspath(path)
    try:
        valid_path = os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        valid_path = False
    if not valid_path:
        print("图片文件缺失、为空或无法访问，图片未提交。")
        return 1

    if target == "master" or (not relay and c.current_target() == "xi" and c.XI_SESSION):
        session = c.MASTER_SESSION if target == "master" else c.XI_SESSION
        c.require(session, f"{target}_session")
        args = ["send", "-p", c.PROJECT, "--session", session, "--image", path]
        if relay:
            # A relay must use the recipient account even if the active key matches.
            c.require(c.MASTER_BOT_ACCOUNT, "master_account_id")
            args.extend(["--account", c.MASTER_BOT_ACCOUNT])
        try:
            result = c.run_cc(args, timeout=60)
        except ValueError:
            print("cc-connect 接收方配置无效，图片未提交。")
            return 1
        except FileNotFoundError:
            print("缺少 cc-connect 可执行文件，图片未提交。")
            return 1
        except (subprocess.TimeoutExpired, OSError):
            return _image_unconfirmed("cc-connect")
        confirmations = (result.stdout or "").strip().splitlines()
        if result.returncode != 0 or confirmations[-1:] != ["Message sent successfully."]:
            return _image_unconfirmed("cc-connect")
        _print_accepted("cc-connect", "图片")
        return 0

    try:
        import send_xi as ilink
    except ImportError:
        print("缺少 iLink 图片发送依赖，图片未提交。")
        return 1
    try:
        result = ilink.send_image(path)
    except ilink.ILinkError as exc:
        if exc.stage == "sendmessage" and exc.outcome == "rejected":
            print("iLink 接口拒绝了图片发送请求，收件待核实。请勿自动重发。")
        elif exc.stage in ("sendmessage", "cc_send"):
            _image_unconfirmed("iLink")
        else:
            print(f"iLink 未提交图片发送请求（{exc.stage}）。")
        return exc.exit_code()
    except Exception as exc:
        return _image_unconfirmed(f"iLink（{type(exc).__name__}）")
    if not isinstance(result, dict) or result.get("outcome") != "accepted":
        return _image_unconfirmed("iLink")
    _print_accepted("iLink", "图片")
    return 0


@c.outbound_serialized
def send_image(target, path, *, relay=False):
    assert_image_target(target, relay=relay)
    code = _send_image_once(target, path, relay=relay)
    if code == 0:
        try:
            c.record_outbound(target)
            c.append_raw("s1" if target == "master" else "s2", "[图片]")
        except (OSError, c.local_state.StateError):
            print("[state] 图片发送接口已接受，本地记录写入失败；请勿自动重发。", file=sys.stderr)
    return code


@c.outbound_serialized
def send_master(text):
    c.require(c.MASTER_SESSION, "master_session（从 session 文件推导）")
    text = c.sanitize_wechat(text)
    try:
        result = c.run_cc([
            "send", "-p", c.PROJECT, "--session", c.MASTER_SESSION, "-m", text,
        ])
    except (subprocess.TimeoutExpired, OSError):
        print('文字发送结果未确认；请先核实送达，不要自动重发。')
        return 1
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "send budget exhausted" in detail.lower() or "budget" in detail.lower():
            print("cc-connect 本地发送保护已触发；先等待并核对状态，不要重新登录或连续重试。")
            return 2
        print(f"文字发送结果未确认（cc-connect 状态 {result.returncode}）；请先核实送达，不要自动重发。")
        return 1
    c.append_raw("s1", text)
    c.append_recent(text)
    _print_accepted("cc-connect", "文字")
    return 0


@c.outbound_serialized
def send_xi(text, relay=False):
    c.require(c.XI_SESSION, "xi_session（从 session 文件推导）")
    text = c.sanitize_wechat(text)
    # Select one transport before sending. A nonzero/unknown cc-connect result
    # may follow a successful send, so never retry it through direct iLink.
    allow_cc = c.current_target() == "xi" or (
        relay and c.context_token_fresh(c.XI_CONTEXT_TOKENS, 30)
    )
    if allow_cc and c.XI_SESSION:
        try:
            first = c.run_cc([
                "send", "-p", c.PROJECT, "--session", c.XI_SESSION, "-m", text,
            ], timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            print("文字发送结果未确认（cc-connect 调用异常）；不会切换通道补发，请先核实送达，不要重试。")
            return 1
        if first.returncode == 0:
            c.append_raw("s2", text)
            c.append_recent(text)
            _print_accepted("cc-connect", "文字")
            return 0
        print("文字发送结果未确认（cc-connect 返回非零状态）；不会切换通道补发，请先核实送达，不要重试。")
        return 1
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_xi.py")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", helper, text],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        print(output or (result.stderr or "").strip()[:200])
        return result.returncode
    c.append_raw("s2", text)
    c.append_recent(text)
    _print_accepted("iLink", "文字")
    return 0


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    relay = "--relay" in args
    if relay:
        args.remove("--relay")
    use_stdin = "--stdin" in args
    if use_stdin:
        args.remove("--stdin")
    if len(args) < 1:
        print('用法: python -X utf8 send_msg.py master|xi [--relay] "消息"')
        print('  或: ... | python -X utf8 send_msg.py master|xi [--relay] --stdin')
        print(IMAGE_USAGE)
        return 1
    target = args[0].lower()
    if any(arg == "--image" or arg.startswith("--image=") for arg in args):
        if use_stdin or len(args) != 3 or args[1] != "--image":
            print(IMAGE_USAGE)
            return 1
        return send_image(target, args[2], relay=relay)
    if use_stdin:
        text = sys.stdin.read().strip()
    else:
        if len(args) < 2:
            print('用法: python -X utf8 send_msg.py master|xi [--relay] "消息"')
            return 1
        text = " ".join(args[1:]).strip()
    if not text:
        print("消息为空")
        return 1
    c.assert_target(target, relay=relay)
    chunks = c.split_wechat_text(text)
    if len(chunks) > 1:
        print(f"[send] 消息超过 {c.WECHAT_TEXT_LIMIT} 字，已拆成 {len(chunks)} 条发送")
    for chunk in chunks:
        if target == "master":
            code = send_master(chunk)
        elif target == "xi":
            code = send_xi(chunk, relay=relay)
        else:
            print("目标必须是 master 或 xi")
            return 1
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())

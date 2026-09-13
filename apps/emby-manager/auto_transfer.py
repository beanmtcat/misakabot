#!/usr/bin/env python3
import json
import hashlib
import hmac
import subprocess
import shlex
import os
import secrets
import sys
import argparse
import tempfile
from pathlib import Path
from datetime import datetime
import time
from typing import Optional, List, Tuple, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

CONFIG_PATH = "/data/media/transfer_config.json"
LOG_PATH = "/data/media/auto_transfer.log"
LOCK_PATH = "/tmp/auto_transfer.lock"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def acquire_lock() -> bool:
    """简单加锁，防止定时任务并发执行"""
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                pid_str = f.read().strip()
            pid = int(pid_str)
            if os.path.exists(f"/proc/{pid}"):
                print(f"Another sync (PID {pid}) is still running, exit.")
                return False
        except Exception:
            pass
        try:
            os.remove(LOCK_PATH)
        except FileNotFoundError:
            pass

    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f"Failed to create lock file: {e}")
        return False
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


def run_cmd_capture(cmd_list: List[str]) -> str:
    """运行命令，返回 stdout 文本，错误抛异常"""
    proc = subprocess.run(
        cmd_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): "
            f"{' '.join(shlex.quote(x) for x in cmd_list)}\n"
            f"STDERR: {proc.stderr}"
        )
    return proc.stdout


def run_cmd_stream(
    cmd_list: List[str],
    monitor_path: Optional[str] = None,
    min_speed_bytes: int = 1 * 1024 * 1024,  # 1 MB/s
    monitor_interval: int = 30,              # 每 30 秒检查一次
    slow_times_threshold: int = 3            # 连续 3 次低速视为“持续慢速”
) -> bool:
    """
    运行命令，实时输出到日志，并监控下载速度。
    monitor_path 不为空时，如果持续低于 min_speed_bytes 达到 slow_times_threshold 次，
    则中断命令并返回 False（保留临时文件，以便下次断点续传）。
    """
    log("RUN: " + " ".join(shlex.quote(x) for x in cmd_list))
    proc = subprocess.Popen(
        cmd_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore"
    )

    def get_size(path: Optional[str]) -> int:
        if not path:
            return 0
        try:
            return os.path.getsize(path)
        except FileNotFoundError:
            return 0
        except Exception:
            return 0

    last_check_time = time.time()
    last_size = get_size(monitor_path)
    slow_count = 0

    try:
        while True:
            raw = proc.stdout.readline()
            if not raw:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            # 单行进度条
            if "\r" in raw:
                segment = raw.split("\r")[-1].rstrip("\n")
                if segment:
                    print("\r" + segment, end="", flush=True)
            else:
                line = raw.rstrip("\n")
                if line:
                    log("  " + line)

            # 速度监控
            if monitor_path:
                now = time.time()
                if now - last_check_time >= monitor_interval:
                    current_size = get_size(monitor_path)
                    delta = current_size - last_size
                    elapsed = now - last_check_time if now > last_check_time else 1
                    speed = delta / elapsed
                    speed_mb = speed / (1024 * 1024)

                    log(
                        f"[SPEED] monitor file={monitor_path}, "
                        f"delta={delta} bytes in {elapsed:.1f}s, "
                        f"avg={speed_mb:.2f} MB/s"
                    )

                    if speed < min_speed_bytes:
                        slow_count += 1
                        log(
                            f"[SPEED] below threshold {min_speed_bytes} B/s "
                            f"({speed_mb:.2f} MB/s), slow_count={slow_count}"
                        )
                        if slow_count >= slow_times_threshold:
                            log(
                                "[SPEED] download speed continuously below threshold, "
                                "terminate process for re-download with resume."
                            )
                            try:
                                proc.terminate()
                            except Exception:
                                pass
                            try:
                                proc.wait(timeout=10)
                            except Exception:
                                pass
                            print()
                            return False
                    else:
                        slow_count = 0

                    last_size = current_size
                    last_check_time = now
    finally:
        print()

    proc.wait()
    if proc.returncode != 0:
        log(f"ERROR: command exit code {proc.returncode}")
        return False
    else:
        log("OK")
        return True


def rclone_obscure(password: str) -> str:
    out = run_cmd_capture(["rclone", "obscure", password])
    return out.strip()


def list_remote_files(webdav_flags: List[str], src_root: str):
    """
    src_root 是 WebDAV 根下的相对目录：
      例如 "欧美剧集/血红海岸 (2023)"
    """
    remote_root = src_root.rstrip("/")
    remote_spec = f":webdav:{remote_root}"
    cmd = ["rclone", "lsjson", remote_spec, "--recursive"]
    cmd.extend(webdav_flags)
    out = run_cmd_capture(cmd)
    data = json.loads(out)
    files = [x for x in data if not x.get("IsDir", False)]
    return files, remote_root


def load_mapping_file(path: str):
    """
    读取 path_map.txt，返回 list[(src_root, dst_root)]
    每行格式：远端目录|本地目录
      远端目录：WebDAV 根下的路径（不带 tv/film）
      本地目录：完整路径，例如 /data/media/tv/欧美剧集/某剧
    """
    mappings = []
    if not os.path.exists(path):
        raise RuntimeError(f"Mapping file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" not in line:
                continue
            src_root, dst_root = line.split("|", 1)
            src_root = src_root.strip().rstrip("/")
            dst_root = dst_root.strip().rstrip("/")
            if src_root and dst_root:
                mappings.append((src_root, dst_root))
    return mappings


def write_mapping_file(path: Path, mappings: list[tuple[str, str]]) -> None:
    """Atomically persist normalized mapping pairs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{src_root.rstrip('/')}|{dst_root.rstrip('/')}" for src_root, dst_root in mappings]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write("\n".join(lines))
        if lines:
            handle.write("\n")
        temporary_path = handle.name
    os.replace(temporary_path, path)


def refresh_mapping_file(cfg: dict, mapping_file: str) -> None:
    """Fetch an HMAC-authenticated mapping from MoviePilot and Emby metadata."""
    api = cfg.get("path_map_api")
    if not isinstance(api, dict):
        return
    url = str(api.get("url") or "").strip()
    node_id = str(api.get("node_id") or "").strip()
    secret = str(api.get("secret") or api.get("token") or "").strip()
    secret_env = str(api.get("secret_env") or api.get("token_env") or "").strip()
    if not secret and secret_env:
        secret = os.environ.get(secret_env, "").strip()
        # Compatibility for the prior configuration, where a secret was placed
        # directly in token_env. New configurations must use secret_env instead.
        if not secret and len(secret_env) >= 32:
            secret = secret_env
    if not url or not node_id or not secret:
        raise RuntimeError("path_map_api requires url, node_id, and secret or secret_env")
    if "{node_id}" not in url:
        raise RuntimeError("path_map_api.url must contain {node_id}")
    endpoint = url.replace("{node_id}", quote(node_id, safe=""))
    timeout = int(api.get("timeout_seconds", 20))
    endpoint_parts = urlsplit(endpoint)
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    signing_payload = "\n".join(
        ("GET", endpoint_parts.path, endpoint_parts.query, timestamp, nonce)
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signing_payload, hashlib.sha256).hexdigest()
    request = Request(
        endpoint,
        headers={
            "Accept": "text/plain",
            # The public endpoint sits behind a WAF that challenges urllib's
            # default user agent before the HMAC-authenticated request reaches it.
            "User-Agent": "Mozilla/5.0",
            "X-Path-Map-Timestamp": timestamp,
            "X-Path-Map-Nonce": nonce,
            "X-Path-Map-Signature": signature,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured HTTPS endpoint
            payload = response.read(5 * 1024 * 1024 + 1)
    except HTTPError as error:
        raise RuntimeError(f"path_map_api returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"path_map_api request failed: {error.reason}") from error
    if len(payload) > 5 * 1024 * 1024:
        raise RuntimeError("path_map_api response exceeds 5 MiB")
    text = payload.decode("utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and any("|" not in line for line in lines):
        raise RuntimeError("path_map_api response has an invalid mapping line")
    remote_mappings = []
    for line in lines:
        src_root, dst_root = line.split("|", 1)
        src_root = src_root.strip().rstrip("/")
        dst_root = dst_root.strip().rstrip("/")
        if src_root and dst_root:
            remote_mappings.append((src_root, dst_root))

    write_mapping_file(Path(mapping_file), remote_mappings)
    log(f"Refreshed mapping file from node {node_id}: {len(remote_mappings)} entries")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Auto transfer files from WebDAV via wget."
    )
    parser.add_argument("--debug", action="store_true", help="wget 详细输出 (-v)")
    parser.add_argument("--single", action="store_true", help="保留参数（仅写日志）")
    parser.add_argument("--limit-rate", type=str, help="wget --limit-rate")
    parser.add_argument("--retry", type=int, help="wget --tries N")
    parser.add_argument(
        "--path",
        help=(
            "临时指定一个目录或文件，例如："
            "\"欧美剧集/血红海岸 (2023)/\" 或 "
            "\"欧美剧集/血红海岸 (2023)/E01.mp4\"；"
            "不带 tv/film 前缀。"
        )
    )
    return parser.parse_args()


def build_mapping_from_path(path_arg: str) -> Tuple[str, str, Optional[str]]:
    """
    根据 --path 构造 (src_root, dst_root, only_file)

    规则：
      - 远端 src_root：完全按你传的路径（去掉首尾 / 和文件名），不加 tv/film。
      - 本地 dst_root：前面加 /data/media/tv 或 /data/media/film
        （根据第一个目录名包含“电影”与否来判断）。
      - only_file：如果是文件则为文件名；目录模式为 None。
    """
    original = path_arg.strip()
    clean = original.strip("/")
    if not clean:
        raise ValueError("Empty --path")

    first_seg = clean.split("/", 1)[0]
    top = "film" if "电影" in first_seg else "tv"

    # 以 / 结尾认为是目录，否则认为是文件
    is_dir = original.endswith("/")

    if is_dir:
        src_root = clean
        dst_root = f"/data/media/{top}/{clean}"
        only_file = None
    else:
        folder = os.path.dirname(clean)  # 可能是 "欧美剧集/血红海岸 (2023)"
        filename = os.path.basename(clean)
        src_root = folder if folder else ""
        if folder:
            dst_root = f"/data/media/{top}/{folder}"
        else:
            dst_root = f"/data/media/{top}"
        only_file = filename

    return src_root, dst_root, only_file


def main():
    args = parse_args()

    if not acquire_lock():
        return

    try:
        if not os.path.exists(CONFIG_PATH):
            print(f"Config not found: {CONFIG_PATH}")
            return

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        webdav = cfg.get("webdav", {})
        url = webdav.get("url")
        user = webdav.get("user")
        password = webdav.get("password")

        if not (url and user is not None and password is not None):
            print("Config 'webdav' section incomplete (url/user/password).")
            return

        include_patterns = cfg.get("include", [])

        # rclone WebDAV 参数（url 是 WebDAV 根，不含 tv/film）
        try:
            obscured_pass = rclone_obscure(password)
        except Exception as e:
            print("Failed to obscure password:", e)
            return

        webdav_flags = [
            f"--webdav-url={url}",
            "--webdav-vendor=other",
            f"--webdav-user={user}",
            f"--webdav-pass={obscured_pass}",
        ]

        # wget 通用参数
        wget_base_opts: List[str] = []
        if args.debug:
            log("Debug mode enabled: wget -v")
            wget_base_opts.append("-v")
        else:
            wget_base_opts.extend(["--progress=bar:force:noscroll"])

        if args.single:
            log("Single-thread flag set (note: wget itself为单线程下载).")
        if args.limit_rate:
            log(f"Limit rate enabled: wget --limit-rate={args.limit_rate}")
            wget_base_opts.append(f"--limit-rate={args.limit_rate}")
        if args.retry is not None:
            log(f"Retry count set: wget --tries {args.retry}")
            wget_base_opts.extend(["--tries", str(args.retry)])

        base_url = url.rstrip("/") + "/"

        # ========= 构造 mappings =========
        mappings: List[Dict[str, Optional[str]]] = []

        if args.path:
            # 临时手工下载：只跑这个 path，不跑 path_map.txt
            try:
                src_root, dst_root, only_file = build_mapping_from_path(args.path)
            except Exception as e:
                print(f"Invalid --path: {e}")
                return

            log(
                f"Use CLI --path mapping: "
                f"src_root='{src_root}', dst_root='{dst_root}', only_file={only_file}"
            )
            mappings.append(
                {"src_root": src_root, "dst_root": dst_root, "only_file": only_file}
            )
        else:
            # 没传 --path：正常走 path_map.txt / mapping_file
            mapping_file = cfg.get("mapping_file")
            if not mapping_file:
                print("Config missing 'mapping_file' and no --path given.")
                return

            try:
                refresh_mapping_file(cfg, mapping_file)
            except Exception as e:
                print("Failed to refresh mapping file:", e)
                return

            try:
                mf_mappings = load_mapping_file(mapping_file)
            except Exception as e:
                print("Failed to load mapping file:", e)
                return

            if not mf_mappings:
                print("No valid mappings in mapping file.")
                return

            for src_root, dst_root in mf_mappings:
                mappings.append(
                    {"src_root": src_root, "dst_root": dst_root, "only_file": None}
                )

        # ========= 开始同步 =========
        for m in mappings:
            src_root = m["src_root"] or ""
            dst_root = m["dst_root"] or ""
            only_file = m.get("only_file")

            name = f"{src_root} -> {dst_root}"
            log(f"[{name}] listing remote files")

            try:
                files, remote_root = list_remote_files(webdav_flags, src_root)
            except Exception as e:
                log(f"[{name}] lsjson failed: {e}")
                continue

            if not files:
                log(f"[{name}] no files found, skip.")
                continue

            # only_file 模式：仅下载指定文件
            if only_file:
                filtered = []
                for f in files:
                    rel = f.get("Path") or f.get("Name")
                    if not rel:
                        continue
                    if rel == only_file or os.path.basename(rel) == only_file:
                        filtered.append(f)
                files = filtered
                if not files:
                    log(f"[{name}] target file not found in remote: {only_file}")
                    continue

            for f in files:
                rel_path = f.get("Path") or f.get("Name")
                if not rel_path:
                    continue

                # include 按后缀过滤
                if include_patterns:
                    rp_lower = rel_path.lower()
                    matched = False
                    for pat in include_patterns:
                        if pat.startswith("*.") and rp_lower.endswith(pat[1:].lower()):
                            matched = True
                            break
                    if not matched:
                        continue

                remote_rel = f"{remote_root}/{rel_path}".lstrip("/")
                remote_url = base_url + remote_rel

                local_file = Path(dst_root) / rel_path
                tmp_file = Path(str(local_file) + ".tmp")
                remote_size = int(f.get("Size", 0))

                # 已有最终文件，认为完成
                if local_file.exists():
                    continue

                local_file.parent.mkdir(parents=True, exist_ok=True)

                if tmp_file.exists():
                    try:
                        tsz = tmp_file.stat().st_size
                    except Exception:
                        tsz = -1
                    log(
                        f"[{name}] found existing tmp for resume: "
                        f"{tmp_file} (size={tsz})"
                    )

                log(f"[{name}] syncing file: {rel_path}")
                log(f"[{name}] URL: {remote_url}")

                cmd = ["wget", "-c"]
                cmd.extend(wget_base_opts)
                cmd.extend([
                    "--http-user", user,
                    "--http-password", password,
                    remote_url,
                    "-O", str(tmp_file)
                ])

                ok = run_cmd_stream(
                    cmd,
                    monitor_path=str(tmp_file),
                    min_speed_bytes=1 * 1024 * 1024,
                    monitor_interval=30,
                    slow_times_threshold=3
                )
                if not ok:
                    log(
                        f"[{name}] download failed, keep tmp for resume: "
                        f"{tmp_file}"
                    )
                    continue

                try:
                    local_size = tmp_file.stat().st_size
                except FileNotFoundError:
                    log(f"[{name}] tmp file missing after wget: {tmp_file}")
                    continue

                if remote_size > 0 and local_size != remote_size:
                    log(
                        f"[{name}] size mismatch for {rel_path}: "
                        f"remote={remote_size}, local={local_size}, "
                        f"keep tmp for next resume."
                    )
                    continue

                try:
                    if local_file.exists():
                        local_file.unlink()
                    tmp_file.rename(local_file)
                    log(f"[{name}] file completed: {local_file}")
                except Exception as e:
                    log(f"[{name}] failed to rename tmp to final: {e}")
                    continue

            log(f"[{name}] sync finished.")

    finally:
        release_lock()


if __name__ == "__main__":
    main()

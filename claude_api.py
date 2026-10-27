"""Đọc quota và gia hạn OAuth nền theo giao thức Claude Code; không gửi prompt."""

import http.client
import json
import math
import os
import socket
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

URL = "https://api.anthropic.com/api/oauth/usage"
# Đối chiếu Claude Code 2.1.293 cài trên máy; đây không phải API công khai.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
POLL_SECONDS = 60


@contextmanager
def refresh_lock(home: Path):
    # proper-lockfile của Claude Code dùng mkdir, cả đường dẫn mới và legacy.
    # CLI heartbeat mỗi 5s; dùng cùng ngưỡng stale 60s để phục hồi sau crash.
    # Timeout 4s mỗi địa chỉ; heartbeat giữ lock cả khi phải thử địa chỉ khác.
    acquired = []
    stopped = threading.Event()
    heartbeat = None

    def owns(path, fd):
        try:
            current, held = path.stat(), os.fstat(fd)
            return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)
        except OSError:
            return False

    def held():
        return all(owns(path, fd) for path, fd in acquired)

    def renew():
        while not stopped.wait(5):
            if not held():
                return
            try:
                for _, fd in acquired:
                    # fd giữ inode cũ; không chạm lock thay thế nếu bị reclaim.
                    os.utime(fd, None)
            except OSError:
                return

    try:
        for path in (home / ".oauth_refresh.lock", Path(str(home.resolve()) + ".lock")):
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                previous = path.stat()
                if time.time() - previous.st_mtime <= 60:
                    raise
                current = path.stat()
                if (current.st_ino, current.st_mtime_ns) != (previous.st_ino, previous.st_mtime_ns):
                    raise
                path.rmdir()
                path.mkdir(mode=0o700)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                path.rmdir()
                raise
            acquired.append((path, fd))
        heartbeat = threading.Thread(target=renew, name="oauth-lock", daemon=True)
        heartbeat.start()
        yield held
    finally:
        stopped.set()
        if heartbeat is not None:
            heartbeat.join()
        for path, fd in reversed(acquired):
            try:
                if owns(path, fd):
                    path.rmdir()
            finally:
                os.close(fd)


def read_oauth(home: Path) -> dict:
    credential = json.loads((home / ".credentials.json").read_text())
    return credential.get("claudeAiOauth", {})


def refresh_oauth(home: Path, opener, failed: dict, now: float) -> dict:
    with refresh_lock(home) as held:
        current = read_oauth(home)
        if (current.get("accessToken"), current.get("refreshToken")) != (
            failed.get("accessToken"),
            failed.get("refreshToken"),
        ):
            return current  # CLI vừa gia hạn hoặc người dùng vừa đổi tài khoản.
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": current["refreshToken"],
            "client_id": current.get("clientId") or CLIENT_ID,
        }
        if current.get("scopes"):
            payload["scope"] = " ".join(current["scopes"])
        request = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if not held():
            raise ConnectionError("Lock gia hạn đã được tiến trình khác thay thế")
        with opener.open(request, timeout=4) as response:
            data = json.load(response)
        access = data.get("access_token")
        refresh = data.get("refresh_token", current["refreshToken"])
        expires = data.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or expires <= 0
        ):
            raise ValueError("Phản hồi gia hạn OAuth không hợp lệ")
        updated = {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": (now + expires) * 1000,
        }
        if isinstance(data.get("scope"), str):
            updated["scopes"] = data["scope"].split()
        # Giữ nguyên metadata và credential khác; không ghi đè tài khoản mới.
        path = home / ".credentials.json"
        credential = json.loads(path.read_text())
        latest = credential.get("claudeAiOauth", {})
        if (latest.get("accessToken"), latest.get("refreshToken")) != (
            current.get("accessToken"),
            current.get("refreshToken"),
        ):
            return latest
        # Như CLI, kể cả lock bị reclaim khi suspend, chỉ lưu token mới nếu
        # token đã POST còn là token hiện tại; giữ mọi metadata mới nhất.
        credential["claudeAiOauth"] = dict(latest, **updated)
        atomic_json(path, credential)
        return credential["claudeAiOauth"]


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".usage-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AddressFallbackHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Proxy CONNECT giữ transport chuẩn; chỉ chọn địa chỉ cho kết nối trực tiếp.
        if self._tunnel_host:
            return super().connect()
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        create_connection = self._create_connection
        try:
            for index, (_, _, _, _, address) in enumerate(addresses):
                host = socket.getnameinfo(address, socket.NI_NUMERICHOST)[0]
                # Chỉ thay socket factory của connection này, không đổi DNS toàn app.
                # Giữ self.host để TLS vẫn kiểm tra hostname/SNI của endpoint gốc.
                self._create_connection = lambda target, timeout, source: create_connection(
                    (host, self.port), timeout, source
                )
                try:
                    super().connect()
                    return
                except ssl.SSLCertVerificationError:
                    self.close()
                    raise
                except OSError:
                    # close() còn reset trạng thái HTTP của request đang gửi.
                    # Chỉ đóng socket lỗi để request tiếp tục sau khi nối lại.
                    if self.sock is not None:
                        self.sock.close()
                        self.sock = None
                    if index == len(addresses) - 1:
                        raise
            raise OSError("Không tìm thấy địa chỉ máy chủ Claude")
        finally:
            self._create_connection = create_connection


class AddressFallbackHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(AddressFallbackHTTPSConnection, request, context=self._context)


def normalize(data: dict, now: float) -> dict:
    if not isinstance(data, dict) or not any(k in data for k in ("five_hour", "seven_day")):
        raise ValueError("Không có trường quota")
    rates = {}
    for key in ("five_hour", "seven_day"):
        value = data.get(key)
        if value is None:
            rates[key] = None
            continue
        pct = value.get("utilization")
        if not isinstance(pct, (float, int)) or not math.isfinite(pct) or pct < 0:
            raise ValueError("Quota không hợp lệ")
        reset = value.get("resets_at")
        if reset is not None:
            reset = (
                datetime.fromisoformat(reset).timestamp()
                if isinstance(reset, str)
                else float(reset)
            )
            if not math.isfinite(reset):
                raise ValueError("Thời điểm reset không hợp lệ")
        rates[key] = {"used_percentage": pct, "resets_at": reset}
    return {"observed_at": now, "rate_limits": rates, "source": "Claude · quota trực tiếp"}


class ClaudeUsageClient:
    def __init__(self, home: Path, state: Path, opener=None):
        self.home = home
        self.cache_path = state / "claude-api-limits.json"
        self.retry_path = state / "claude-api-retry.json"
        self.opener = opener or urllib.request.build_opener(NoRedirect, AddressFallbackHTTPSHandler)
        self.snapshot = None
        self.next_poll = 0.0
        self.failures = 0
        self.error = None
        try:
            value = json.loads(self.cache_path.read_text())
            if isinstance(value, dict) and isinstance(value.get("rate_limits"), dict):
                self.snapshot = value
                observed = value.get("observed_at", 0)
                if isinstance(observed, (float, int)) and math.isfinite(observed):
                    self.next_poll = observed + POLL_SECONDS
        except (OSError, ValueError):
            pass
        try:
            retry = json.loads(self.retry_path.read_text())
            deadline = retry.get("next_poll")
            if isinstance(deadline, (float, int)) and math.isfinite(deadline):
                self.next_poll = max(self.next_poll, deadline)
                self.error = "Claude HTTP 429; đang chờ hết thời gian giới hạn máy chủ."
        except (OSError, ValueError, AttributeError):
            pass

    def _usage(self, token):
        request = urllib.request.Request(
            URL,
            headers={
                "Authorization": "Bearer " + token,
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
                "User-Agent": "ai-usage-tray/0.1.0",
            },
        )
        with self.opener.open(request, timeout=4) as response:
            return json.load(response)

    def poll(self, now=None):
        live_clock = now is None
        now = time.time() if now is None else now
        if now < self.next_poll:
            return self.snapshot, self.error
        self.next_poll = now + POLL_SECONDS
        try:
            oauth = read_oauth(self.home)
            refreshed = False
            expires = oauth.get("expiresAt")
            if (
                oauth.get("refreshToken")
                and isinstance(expires, (float, int))
                and math.isfinite(expires)
                and expires / 1000 <= now + 300
            ):
                refreshed = True
                try:
                    oauth = refresh_oauth(self.home, self.opener, oauth, now)
                except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                    if (
                        expires / 1000 <= now
                        or not oauth.get("accessToken")
                        or isinstance(exc, urllib.error.HTTPError)
                        and exc.code == 429
                    ):
                        raise
                    if isinstance(exc, urllib.error.HTTPError):
                        exc.close()
                    # Gia hạn sớm có thể lỗi trong khi bearer hiện tại còn dùng được.
            token = oauth.get("accessToken")
            if not token:
                self.error = "Claude: chưa đăng nhập. Chạy `claude auth login` để kết nối lại quota."
                return self.snapshot, self.error
            try:
                data = self._usage(token)
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or refreshed or not oauth.get("refreshToken"):
                    raise
                exc.close()
                oauth = refresh_oauth(self.home, self.opener, oauth, now)
                data = self._usage(oauth["accessToken"])
            self.snapshot = normalize(data, time.time() if live_clock else now)
            self.failures = 0
            self.error = None
            self.next_poll = self.snapshot["observed_at"] + POLL_SECONDS
            try:
                self.retry_path.unlink(missing_ok=True)
                atomic_json(self.cache_path, self.snapshot)
            except OSError:
                self.error = "Claude: đọc quota thành công nhưng chưa lưu được cache."
        except FileNotFoundError:
            self.error = "Claude: chưa có đăng nhập cục bộ. Chạy `claude auth login` để kết nối quota."
        except urllib.error.HTTPError as exc:
            self.failures += 1
            delay = min(900, 60 * 2 ** min(self.failures - 1, 4))
            if exc.code >= 500:
                delay = min(30, 5 * 2 ** min(self.failures, 3))
            elif exc.code in (400, 401, 403):
                delay = 60
            if exc.code == 429:
                try:
                    retry = exc.headers.get("Retry-After", "")
                    delay = max(
                        delay,
                        float(retry)
                        if retry.isdigit()
                        else parsedate_to_datetime(retry).timestamp() - now,
                    )
                except (ValueError, TypeError, AttributeError, OverflowError):
                    pass
            if exc.code in (400, 401, 403):
                self.error = (
                    f"Claude HTTP {exc.code}: không gia hạn được phiên; "
                    "đăng nhập lại bằng `claude auth login`."
                )
            else:
                self.error = f"Claude HTTP {exc.code}; thử lại sau {int(delay)} giây."
            self.next_poll = now + delay
            if exc.code == 429:
                try:
                    atomic_json(self.retry_path, {"next_poll": self.next_poll})
                except OSError:
                    pass
            exc.close()
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            self.failures += 1
            delay = min(30, 5 * 2 ** min(self.failures, 3))
            self.next_poll = now + delay
            self.error = f"Claude: chưa cập nhật được quota; thử lại sau {delay} giây."
        return self.snapshot, self.error

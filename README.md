# AI Usage Tray

Tiện ích Linux chạy nền ở system tray, tự cập nhật quota qua mạng **không cần mở
terminal hay phiên chat**. Quota Claude lấy mỗi 60 giây, Codex mỗi 5 giây;
giao diện và bộ đếm token cục bộ cập nhật mỗi 5 giây.
Hai biểu tượng **CL** và **CX** hiển thị phần trăm hạn mức ngắn hạn đã dùng.
Bấm biểu tượng để xem bảng chi tiết; chuột phải để quét ngay hoặc thoát.
Đóng cửa sổ chỉ ẩn bảng, ứng dụng vẫn chạy nền.

## Cấu hình phần trăm và màu

Chuột phải **CL/CX → Cấu hình hiển thị…**, hoặc bấm **Cấu hình** trong bảng usage:

- Bật/tắt phần trăm trực tiếp trên biểu tượng khay ở taskbar.
- Chọn **% đã dùng / % còn lại** và **hạn mức ngắn hạn / dài hạn**.
- Tắt nhãn CL/CX để phần trăm hiện lớn hơn; khi tắt phần trăm, hiện nhãn CL/CX.
- Chọn màu chữ/số riêng cho Claude, Codex và màu nền biểu tượng.
- Xem trước, rồi **Lưu và áp dụng** ngay, không cần khởi động lại.

Màu nhà cung cấp cũng áp dụng cho thanh usage trong cửa sổ. Số thiếu dữ liệu vẫn
hiện `—`, không coi là 0%. Màu được chọn giữ nguyên, không tự đổi theo ngưỡng usage.
Cấu hình lưu tại `~/.config/ai-usage-tray/config.json` (tôn trọng `XDG_CONFIG_HOME`)
và được đọc lại khi mở ứng dụng. Nếu sửa JSON thủ công, khởi động lại để áp dụng:

```json
{
  "show_percent": true,
  "show_label": false,
  "percentage_mode": "remaining",
  "period": "primary",
  "colors": {"claude": "#df9a78", "codex": "#65d5ba"},
  "background_color": "#182231"
}
```

Đây là biểu tượng trong system tray của panel Linux. Nếu KDE ẩn biểu tượng,
vào cấu hình **System Tray → Entries** và chọn **Always shown** cho ứng dụng.

## Alarm reset 5 giờ

Mặc định bật thông báo desktop kèm âm thanh cho Claude và Codex:

- **Đến giờ dự kiến reset:** nhắc theo `resets_at`, ghi rõ chưa xác nhận về 0%.
- **Đã reset về 0%:** chỉ báo khi có snapshot mới sau mốc reset xác nhận `used_percent = 0`.
- Mỗi loại thông báo chỉ phát một lần trong một cửa sổ; lưu trạng thái qua lần khởi động lại.
- Cửa sổ chưa phát sinh usage và log lịch sử từ trước khi mở app không tự tạo alarm.

Trong **Cấu hình**, bật/tắt alarm, âm thanh, hoặc chỉ giữ thông báo xác nhận.
Chuột phải biểu tượng → **Thử thông báo reset** để kiểm tra notification/loa.
Desktop có thể ẩn thông báo khi bật Do Not Disturb. Cả Claude và Codex đều đọc quota
trực tiếp để xác nhận 0%, kể cả khi không có phiên chat đang ghi log. Nếu mạng/auth
gặp lỗi thì chỉ có lời nhắc theo giờ, không tự biến số hiện tại thành 0%.
Alarm báo reset phần trăm quota; bộ đếm token hôm nay vẫn được giữ nguyên.

## Cài và chạy

Cần Python 3.12+, `uv`, desktop có system tray (KDE Plasma/Wayland đã kiểm tra).
GNOME cần extension hỗ trợ AppIndicator/StatusNotifier. Máy headless dùng `--once`.

### Chạy ngầm bằng systemd (khuyến nghị trên desktop Linux)

```bash
cd ~/KentProject/ai-usage-tray
python3 install.py --service --start
# Nếu Python hệ thống chưa có PyQt6:
# uv sync --locked
# uv run python install.py --service --start
```

Service chạy theo user, không cần root, tự chạy khi đăng nhập desktop, dừng khi
phiên đồ họa kết thúc và tự khởi động lại sau 10 giây nếu ứng dụng lỗi. Không cần
giữ terminal mở. Vẫn có biểu tượng CL/CX, cửa sổ chi tiết và thông báo reset.
Chọn **Thoát** trong khay sẽ dừng ứng dụng bình thường; dùng `start` để mở lại.

Installer tạo `~/.config/systemd/user/ai-usage-tray.service`, bật service và gỡ
XDG autostart cũ để tránh chạy trùng. Nếu đang chạy bản cũ ngoài service, hãy thoát
từ menu khay trước khi cài. Chạy lại installer sẽ giữ chế độ service đã cài;
`--start` cũng khởi động lại service để nhận thay đổi mã nguồn/cấu hình.

```bash
systemctl --user status ai-usage-tray.service
systemctl --user restart ai-usage-tray.service
systemctl --user stop ai-usage-tray.service
systemctl --user start ai-usage-tray.service
journalctl --user -u ai-usage-tray.service -f
# Tắt tự khởi chạy và dừng ngay:
systemctl --user disable --now ai-usage-tray.service
# Bật lại:
systemctl --user enable --now ai-usage-tray.service
```

Service cần phiên desktop có `graphical-session.target` (ví dụ KDE Plasma) và
môi trường Wayland/X11 được desktop truyền vào systemd user manager. Chạy lệnh
cài từ terminal trong desktop để `--start` nhập các biến hiển thị hiện tại.
Các đường dẫn tùy chỉnh (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `XDG_*`) và `PATH`
được ghi vào unit lúc cài; chạy lại installer nếu đổi chúng. Unit trỏ đến Python
và source hiện tại: giữ nguyên thư mục dự án/venv, hoặc cài lại sau khi di chuyển.

Đây là ứng dụng tray cần desktop; service không chạy GUI trước khi đăng nhập
hoặc trên máy headless. Docker chưa được cấu hình vì để giữ khay và thông báo
cần nối thêm display, D-Bus cùng dữ liệu đăng nhập trên host. Với chức năng hiện
tại, systemd user service dùng trực tiếp các tài nguyên desktop này.

### Cài bằng XDG autostart (không dùng systemd)

Nếu Python hệ thống đã có PyQt6 (như máy KDE hiện tại), dùng trực tiếp, không cần
tải thêm Qt hoặc tạo môi trường:

```bash
cd ~/KentProject/ai-usage-tray
python3 install.py --start
```

Nếu chưa có PyQt6, dùng môi trường riêng bằng uv:

```bash
cd ~/KentProject/ai-usage-tray
uv sync
uv run python install.py --start
```

Installer tạo launcher `~/.local/bin/ai-usage-tray`, Desktop Entry và XDG autostart
để chạy lại khi đăng nhập. Không cần root. Chạy lại installer được.
Chỉ một instance GUI được chạy; chọn biểu tượng trong khay để mở instance hiện tại.

```bash
ai-usage-tray                 # Chạy nền trong khay
ai-usage-tray --show          # Mở cửa sổ nếu chưa có instance đang chạy
ai-usage-tray --once          # JSON, không cần giao diện
uv run python -m unittest -v  # Kiểm tra bộ đọc và bridge
uv run ruff check .
```

## Dữ liệu thực sự được đo

| Mục | Nguồn | Giới hạn |
|---|---|---|
| Codex: hạn mức và giờ reset | `account/rateLimits/read` qua Codex app-server | Đọc trực tiếp bucket `codex`; không phải quota chat trên web ChatGPT |
| Claude: hạn mức 5 giờ/7 ngày | GET `https://api.anthropic.com/api/oauth/usage` bằng đăng nhập Claude Code hiện có | Đọc trực tiếp cả lúc chạm 100%; cần mạng và token đăng nhập còn hiệu lực |
| Token hôm nay | Log Claude Code và Codex trên máy | Ngày theo timezone hệ thống; không bao gồm web hoặc máy khác |

Claude dùng hạn mức chung cho các giao diện Claude theo chính sách tài khoản;
công cụ hiển thị quota tài khoản, không tách riêng usage web với CLI.
**Quét 5 giây gồm đọc log token cục bộ và kiểm tra lịch lấy quota trực tiếp song song.**
Claude lấy quota mỗi 60 giây để giảm HTTP 429; Codex lấy mỗi 5 giây.
Sau lỗi kết nối, cả hai tự thử lại với backoff tối đa 30 giây khi ứng dụng còn chạy.
HTTP 429 tôn trọng thông tin chờ máy chủ cung cấp và backoff; lỗi mạng/auth giữ snapshot cũ kèm lỗi.
Claude lưu thời gian chờ 429 qua lần khởi động lại; quét tay vẫn tôn trọng lịch này.
Endpoint Claude được đối chiếu với Claude Code cài trên máy, không phải contract API công khai
ổn định. Claude tự gia hạn bằng refresh token đã lưu khi access token sắp hết hạn
(trước 5 phút), hoặc thử gia hạn một lần khi máy chủ trả HTTP 401. Không cần mở
Claude Code để gia hạn. Cần đăng nhập lần đầu; nếu refresh token bị thu hồi/hết hạn
hoặc máy chủ từ chối gia hạn thì vẫn cần đăng nhập lại.
Giao diện hiện thời điểm nhận snapshot và đánh dấu dữ liệu cũ sau 2 phút.
Khi qua giờ reset mà chưa có snapshot mới, giữ số gần nhất (kể cả **100%**) và ghi
“chờ dữ liệu mới”; không giả định đã về 0%. Chế độ **% còn lại** vẫn hiển thị 0% khi đã dùng 100%.

Token input đã gồm cache đọc/ghi; output reasoning nằm trong output nếu nguồn đã gộp.
Không cộng thêm cache hoặc reasoning một lần nữa. Claude deduplicate bằng message ID.
Codex lấy chênh lệch bộ đếm cộng dồn trong phiên. Nếu log bắt đầu giữa phiên hoặc
fork mang theo bộ đếm kế thừa, snapshot đầu có thể gồm usage trước đó; đây là thống kê
log quan sát được, **không dùng thay hóa đơn hoặc audit theo ngày**. Không ước lượng tiền
vì không có bảng giá đã xác thực cho mọi model/plan trên máy.

## Quota Codex trực tiếp

Tool giữ một tiến trình `codex app-server --listen stdio://`, handshake một lần rồi
gửi `account/rateLimits/read` mỗi 5 giây. Codex CLI quản lý đăng nhập hiện có;
monitor không tự đọc hoặc sao chép token Codex. Không tạo thread/turn, không gửi
prompt và không tiêu thụ reset credit. App-server đóng cùng ứng dụng; timeout/crash
được dọn tiến trình và thử lại theo backoff.

Ưu tiên `rateLimitsByLimitId.codex`, hoặc bucket `rateLimits` tương thích. Ánh xạ
`usedPercent`, `windowDurationMins`, `resetsAt` theo dữ liệu trả về, không giả định mọi
primary đều là 5 giờ. Cache ở `codex-api-limits.json`; log quota chỉ làm nguồn dự
phòng nếu chưa có snapshot trực tiếp. Giao diện luôn ghi nguồn và thời điểm lấy dữ liệu.

Cần Codex CLI trong PATH hoặc `~/.local/bin/codex`, đã đăng nhập tài khoản ChatGPT.
Giao thức đối chiếu theo [OpenAI App Server docs](https://learn.chatgpt.com/docs/app-server).

## Claude statusLine bridge

Nếu chưa có `statusLine`, installer thêm bridge với `refreshInterval: 5` vào
`CLAUDE_CONFIG_DIR/settings.json` (mặc định `~/.claude/settings.json`). Settings cũ
được sao lưu trong `~/.local/state/ai-usage-tray/claude-settings.before-tray.json`.
Bridge là nguồn dự phòng khi chưa có snapshot API; không bắt buộc khi đã đọc quota
trực tiếp thành công. Không có snapshot thì hiện “chưa có dữ liệu”, không biến thành 0%.

Nếu đã có statusLine riêng, installer giữ nguyên. Có thể nối bridge vào script hiện
có: đọc stdin một lần vào biến, chuyển JSON cho `ai-usage-claude-bridge >/dev/null`,
rồi chuyển cùng JSON cho script statusLine cũ. Project settings có thể override
user settings; kiểm tra cấu hình project nếu bridge chưa nhận dữ liệu.

## Vận hành và gỡ

- Đọc log tăng dần; file đang ghi dở được đọc tiếp ở chu kỳ sau.
- Cold start đọc toàn bộ log để lấy counter nền; các lượt sau đọc phần nối thêm.
- Worker tách khỏi UI. Nếu lần quét mất hơn 5 giây, bỏ lượt chồng chéo.
- Đọc `claudeAiOauth` từ `CLAUDE_CONFIG_DIR/.credentials.json` để gọi quota và
  gia hạn tại `https://platform.claude.com/v1/oauth/token` qua HTTPS; từ chối redirect.
- Token sau gia hạn được ghi nguyên tử vào chính file đăng nhập với quyền `0600`,
  giữ metadata và credential khác. Dùng lock tương thích Claude Code để tránh
  cùng xoay token; kiểm tra lại tài khoản trước khi ghi. Lock cũ sau crash được
  xử lý theo ngưỡng 60 giây của Claude Code.
- Không đọc cookie trình duyệt, không gửi hội thoại, không lưu/in token đăng nhập.
- Bridge chỉ lưu `rate_limits` và timestamp, quyền file `0600`.
- Cache API `claude-api-limits.json`, `codex-api-limits.json` và dấu alarm `reset-alarms.json` trong thư mục
  state cũng dùng quyền `0600`, chỉ chứa quota và thời điểm.
- `claude-api-retry.json` chỉ lưu thời điểm được phép thử lại sau HTTP 429.
- Tôn trọng `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `XDG_STATE_HOME`, `XDG_CONFIG_HOME`,
  `XDG_DATA_HOME`; các biến tùy chỉnh cần được truyền vào phiên desktop/autostart.
- Log khi dùng systemd: `journalctl --user -u ai-usage-tray.service`.
- Log khi dùng XDG autostart qua installer `--start`: `~/.local/state/ai-usage-tray/app.log`.

```bash
cd ~/KentProject/ai-usage-tray
uv run python install.py --uninstall
```

Installer dừng và gỡ service nếu đã cài. Với XDG autostart, thoát từ menu khay.
Gỡ bỏ launcher/autostart và trường statusLine nếu vẫn trỏ đúng
bridge này; giữ nguyên các settings khác, source code và bản sao lưu.

Nguồn tham khảo: [Claude statusLine](https://code.claude.com/docs/en/statusline),
[Claude usage limits](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work),
[Qt system tray](https://doc.qt.io/qt-6/qsystemtrayicon.html).

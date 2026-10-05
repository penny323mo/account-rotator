# Account Rotator

一個 macOS app，幫你管理同輪換多個 AI coding 工具帳號：**Antigravity（agy）**、**Codex（ChatGPT）**、**Claude Code**。
睇晒每個帳號嘅 5 小時同每週用量，用完自動轉去有額度嘅帳號，閒置帳號自動「喚醒」開始計時。

A macOS app that manages and rotates several accounts of AI coding tools — **Antigravity (agy)**, **Codex (ChatGPT)**
and **Claude Code**: see every account's 5-hour and weekly usage, switch to an account with capacity when the live one
runs out, and start idle accounts' usage clocks.

> 帳號資料（登入 token）只會儲存喺你部 Mac 嘅 Keychain，唔會上傳去任何地方。
> Sign-ins (tokens) are stored only in this Mac's Keychain and are never uploaded anywhere.

## 注意 · Disclaimer

- 本 app 同 Google、OpenAI、Anthropic 冇任何關係。查用量用嘅係呢幾間公司冇公開承諾嘅接口，佢哋隨時可以改，令某部分功能停用。
  This app is not affiliated with Google, OpenAI or Anthropic. Usage is read through interfaces they do not
  publish or promise to keep; they may change at any time and break parts of the app.
- 用多個帳號輪流避開用量上限，可能違反呢幾間公司嘅使用條款，帳號有機會被限制或者停用。**風險由用家自己承擔。**
  Rotating several accounts to get past usage limits may break these companies' terms of service, and accounts may
  be limited or suspended. **Use at your own risk.**
- 「自動喚醒」會用你嘅帳號真係發一句短 prompt，會用少少額度。
  Waking an idle account really sends a short prompt with that account and uses a little of its quota.

## 功能 · Features

| | Antigravity | Codex | Claude Code |
|---|---|---|---|
| 用量（5 小時 / 每週）· Usage (5 h / weekly) | ✓ | ✓ | ✓ |
| 加帳號（瀏覽器登入）· Add account (browser sign-in) | ✓ | ✓ | ✓ |
| 手動切換 · Manual switch | ✓ | ✓ | ✓ |
| 用盡自動轉 · Auto-switch when used up | ✓ | ✓ | — |
| 轉帳號後自動接續對話 · Continue after a switch | 提供接返指令 · resume command | ✓ | — |
| 自動喚醒閒置帳號 · Wake idle accounts | ✓ | ✓ | ✓ |
| 一鍵重設 · One-tap reset | ✓ | ✓ | ✓ |

**轉帳號時會做咩 · What a switch does**

- **Antigravity**：關閉閒置嘅 agy（任何終端機），agy 會先儲存對話；轉完會通知你一句 `agy --conversation=…` 指令接返同一個對話。做緊嘢嘅 agy 唔會被關。
  Idle agy sessions (in any terminal) are closed after agy saves the conversation; you get the `agy --conversation=…`
  command to continue it. A session in the middle of a turn is never closed.
- **Codex**：關閉 ChatGPT app 同 Codex CLI、轉帳號、再重開 ChatGPT。
  ChatGPT and the Codex CLI are closed, the account is switched, and ChatGPT is reopened.
- **Claude Code**：直接換登入，行緊嘅 Claude Code 約 30 秒內自動跟住轉，唔使關。
  The login is swapped; running Claude Code sessions pick it up within ~30 s without being closed.

## 要求 · Requirements

- macOS 13 或以上 · macOS 13 or later
- Command Line Tools（提供 `python3` 同 `swift`）· Command Line Tools (for `python3` and `swift`):
  ```zsh
  xcode-select --install
  ```
- 你想管理嘅工具（裝咗邊個就用邊個）· The tools you want to manage (only the installed ones are used):
  `agy` CLI、ChatGPT app / Codex CLI、Claude Code

## 安裝 · Install

**用 .dmg（建議）· From the .dmg (recommended)**

1. 打開 `Account-Rotator-<版本>.dmg`，將 **Account Rotator** 拖去 **Applications**。
   Open the .dmg and drag **Account Rotator** onto **Applications**.
2. 第一次開：app 冇 Apple 公證，macOS 會話「無法驗證開發者」。去 **系統設定 → 私隱與安全性**，喺下面撳 **仍要打開**。
   The app is not notarized, so macOS blocks the first launch: go to **System Settings → Privacy & Security** and
   click **Open Anyway**.
3. 如果未裝 Command Line Tools，app 會彈出提示，撳「安裝」就得；裝完再開一次 app。
   Without the Command Line Tools the app offers to install them; open the app again afterwards.

App 開啟時會將程式抄去 `~/Library/Application Support/Account Rotator/`，再登記兩個背景服務（開機自動行）：
`io.account-rotator.daemon`（查用量、轉帳號）同 `io.account-rotator.web`（控制台，只開放畀本機 `127.0.0.1:3082`）。
macOS 會通知「已加入背景項目」。之後搬動或者刪除 app 都唔會影響背景服務；裝新版再開一次 app，就會自動換新程式並重啟服務
（換嘅時候控制台會有十幾秒顯示「連線中斷」）。

At launch the app copies the rotator to `~/Library/Application Support/Account Rotator/` and registers two login
services that run that copy: `io.account-rotator.daemon` (usage and switching) and `io.account-rotator.web` (the
console, on `127.0.0.1:3082` only). macOS shows a "Background Items Added" notice. Moving or deleting the app does not
affect them; opening a newer version replaces the copy and restarts them (the console is offline for a few seconds).

**由原始碼 · From source**

```zsh
git clone https://github.com/penny323mo/account-rotator.git ~/account-rotator
cd ~/account-rotator
scripts/install.sh      # build → ~/Applications/Account Rotator.app → open
scripts/make-dmg.sh     # build → dist/Account-Rotator-<version>.dmg
```

## 第一次用 · First run

1. 打開 Account Rotator（menu bar 會出現圖示）→「開啟控制台」。
   Open Account Rotator (it lives in the menu bar) → 開啟控制台 (Open console).
2. 三個分頁一開始都係空嘅。揀分頁 →「＋ 加帳號」→ 用瀏覽器登入。建議用私人瀏覽視窗，咁唔會影響你瀏覽器入面已經登入咗嘅帳號。
   Every tab starts empty. Pick a tab → ＋ 加帳號 (Add account) → sign in in the browser, ideally in a private window
   so the accounts already signed in there are not affected.
3. 加夠兩個或以上帳號，就可以開「用盡自動轉」。
   With two or more accounts, turn on auto-switching.

## 手機遙控 · Phone remote control

預設關閉。喺 Mac 撳控制台右上角嘅手機圖示（或者 menu bar →「配對手機」）→ 開「開啟手機遙控」→ 揀連接方式 →
「配對手機」，用手機相機掃 QR code，或者喺手機瀏覽器開顯示嘅網址再輸入配對碼。配對碼 5 分鐘內有效，只可以用一次。

Off by default. On the Mac, click the phone icon at the top right of the console (or menu bar → 配對手機), turn on
remote control, pick how the phone connects, then 配對手機: scan the QR code with the phone's camera, or open the
shown address on the phone and type the code. A code works once, within 5 minutes.

| 連接方式 · Connection | 點設定 · Setup |
|---|---|
| 同一個 Wi-Fi · Same Wi-Fi | 開「同一個 Wi-Fi」。普通 HTTP、冇加密：只喺屋企 Wi-Fi 用。· Turn on 同一個 Wi-Fi. Plain HTTP: home Wi-Fi only. |
| Tailscale | Mac 同手機都裝 Tailscale 並登入同一個帳號，再開「Tailscale」。出街都用到，連線由 Tailscale 加密。· Install Tailscale on both and sign in to the same account, then turn on Tailscale. Works anywhere, encrypted by Tailscale. |
| 自己嘅公開網址 · Your own public address | 用 ngrok、cloudflared 之類將 tunnel 指去 **`127.0.0.1:3083`**（唔係 3082），再將佢嘅網址填入「公開網址」（偵測到 ngrok 會自動提議），例如 `ngrok http 3083`。· Point an ngrok / cloudflared tunnel at **`127.0.0.1:3083`** (not 3082) and enter its address (a running ngrok is detected), e.g. `ngrok http 3083`. |

- 配對咗嘅手機預設只可以睇用量、轉帳號同暫停，睇唔到帳號 email。想用手機加帳號、改設定，要喺 Mac 開「手機可以加帳號、改設定」。
  配對同移除裝置永遠只可以喺 Mac 做。
  A paired phone may only view usage, switch and pause, and never sees account e-mail addresses; turn on full control
  on the Mac to add accounts or change settings from it. Pairing and removing devices happen on the Mac only.
- 配對咗嘅手機就等於你本人：手機唔見咗，即刻喺 Mac 嘅「已配對裝置」移除佢。90 日冇用過嘅裝置會自動失效。
  A paired phone acts as you: if it is lost, remove it on the Mac right away. Devices unused for 90 days expire.
- 用公開網址嘅話，ngrok 一類 tunnel 服務會睇到經過嘅內容。· With a public address, the tunnel service can see the traffic.
- 3083 係專畀 tunnel 用嘅 port：經佢入嚟嘅一律當遠端，要配對先用得。**唔好將 tunnel 指去 3082**；經 proxy 或 tunnel 入嚟嘅 request（帶
  `X-Forwarded-For`、`Forwarded`、`Via` 之類）喺 3082 會被拒絕。
  Port 3083 is for tunnels: everything arriving there is remote and needs pairing. **Do not point a tunnel at 3082**;
  proxied requests (with `X-Forwarded-For`, `Forwarded`, `Via` …) are refused there.
- 自己架嘅反向 proxy（例如 nginx 嘅 `proxy_pass`）都一樣要指去 3083：nginx 預設唔加上面嗰啲 header，指去 3082 就會被當成呢部 Mac
  本身，唔使配對就用得。· Your own reverse proxy (e.g. nginx `proxy_pass`) must also point at 3083: nginx adds none of
  those headers by default, so pointed at 3082 it would pass for this Mac without pairing.

## 安全 · Security

- 手機遙控關閉時，控制台只聽 `127.0.0.1`，只有呢部 Mac 用得。開咗之後，除咗呢部 Mac，所有連線都要係配對過嘅裝置。
  With remote control off the console listens on `127.0.0.1` only. With it on, everything except this Mac must come
  from a paired device.
- 唔好用 port forwarding 將 3082 開放畀成個互聯網；要喺外面用，就用 Tailscale 或者 tunnel 加配對。
  Do not port-forward 3082 to the internet; from outside, use Tailscale or a tunnel together with pairing.

## 移除 · Uninstall

```zsh
for L in io.account-rotator.daemon io.account-rotator.web; do
  launchctl bootout "gui/$(id -u)/$L"; rm -f ~/Library/LaunchAgents/$L.plist
done
rm -rf "/Applications/Account Rotator.app" ~/Applications/"Account Rotator.app" \
       ~/Library/Application\ Support/"Account Rotator"
```

設定同紀錄喺 `~/.agy-rotator`，已登記嘅帳號喺 `~/.agy-account`、`~/.codex-account`、`~/.claude-account` 同 Keychain（服務名
`agy-account-broker`、`codex-account-broker`、`claude-account-broker`），要清就自己刪。

Settings and logs are in `~/.agy-rotator`; enrolled accounts are in `~/.agy-account`, `~/.codex-account`,
`~/.claude-account` and the Keychain (services `agy-account-broker`, `codex-account-broker`, `claude-account-broker`).
Delete them yourself to remove everything.

## 開發 · Development

```zsh
python3 -m unittest discover -s tests            # daemon
(cd helpers && python3 -m unittest discover -p 'test_*.py')   # account helpers
scripts/build.sh                                  # app → dist/
```

- `daemon/`：背景服務（Python 標準庫，冇第三方依賴）· the background service (standard library only)
- `helpers/`：`agy-account`、`codex-account`、`claude-account`，每個都可以獨立喺命令列用 · each also usable on its own
- `web/`：控制台 · the console
- `app/`：Swift menu bar app

測試唔會掂真實帳號、Keychain 或者進程。· Tests never touch real accounts, the Keychain or processes.

# Account Rotator

一個 macOS app，幫你管理同輪換多個 AI coding 工具帳號：**Antigravity（agy）**、**Codex（ChatGPT）**、**Claude Code**。
睇晒每個帳號嘅 5 小時同每週用量，用完自動轉去有額度嘅帳號，閒置帳號自動「喚醒」開始計時。

A macOS app that manages and rotates several accounts of AI coding tools — **Antigravity (agy)**, **Codex (ChatGPT)**
and **Claude Code**: see every account's 5-hour and weekly usage, switch to an account with capacity when the live one
runs out, and start idle accounts' usage clocks.

> 帳號資料（登入 token）只會儲存喺你部 Mac 嘅 Keychain，唔會上傳去任何地方。
> Sign-ins (tokens) are stored only in this Mac's Keychain and are never uploaded anywhere.

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

```zsh
git clone <this repository> ~/account-rotator
cd ~/account-rotator
scripts/install.sh
```

會 build app 去 `~/Applications/Account Rotator.app`，並登記兩個背景服務（開機自動行）：
`io.account-rotator.daemon`（查用量、轉帳號）同 `io.account-rotator.web`（控制台，只開放畀本機 `127.0.0.1:3082`）。

This builds `~/Applications/Account Rotator.app` and registers two login services: `io.account-rotator.daemon`
(usage and switching) and `io.account-rotator.web` (the console, on `127.0.0.1:3082` only).

## 第一次用 · First run

1. 打開 Account Rotator（menu bar 會出現圖示）→「開啟控制台」。
   Open Account Rotator (it lives in the menu bar) → 開啟控制台 (Open console).
2. 三個分頁一開始都係空嘅。揀分頁 →「＋ 加帳號」→ 用瀏覽器登入。建議用私人瀏覽視窗，咁唔會影響你瀏覽器入面已經登入咗嘅帳號。
   Every tab starts empty. Pick a tab → ＋ 加帳號 (Add account) → sign in in the browser, ideally in a private window
   so the accounts already signed in there are not affected.
3. 加夠兩個或以上帳號，就可以開「用盡自動轉」。
   With two or more accounts, turn on auto-switching.

## 安全 · Security

- 控制台只聽 `127.0.0.1`，自己冇登入功能：**唔好用 tunnel 或者 port forwarding 將 3082 公開出去**。
  The console listens on `127.0.0.1` only and has no login of its own: **never expose port 3082 through a tunnel or port
  forwarding**.
- 手機遙控（連登入同配對）係之後嘅功能。· Remote control from a phone (with its own login and pairing) is planned.

## 移除 · Uninstall

```zsh
for L in io.account-rotator.daemon io.account-rotator.web; do
  launchctl bootout "gui/$(id -u)/$L"; rm -f ~/Library/LaunchAgents/$L.plist
done
rm -rf ~/Applications/"Account Rotator.app"
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

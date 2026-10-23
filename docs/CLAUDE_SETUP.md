# 新電腦設置指南（給 Claude Code）

這份文件寫給 Claude Code 讀。使用者在新電腦下載 Video Console（以下簡稱 VC）之後，把下面這句話交給 Claude Code 即可：

> 請讀 `<VC 資料夾>\docs\CLAUDE_SETUP.md`，照著幫我把 Video Console 設置好，讓你之後可以直接操作它。

以下是給 Claude 的步驟。每一步做完確認結果再往下；需要下載或安裝東西時先跟使用者說明檔名、來源、大小並取得同意。

## 步驟 0：認識 VC

- VC 是本機影片剪輯工具，介面跑在瀏覽器視窗，後端是 Python 標準庫寫的 HTTP 伺服器，只聽 `127.0.0.1:8790`（被占用時往後到 8799）。
- 所有操作都能透過 API 完成，完整說明在 `docs/API.md`，用戶端在 `tools/vc_client.py`（只用標準庫）。
- 有兩種形式：
  - **執行檔版**：資料夾裡有 `Video Console.exe` 與 `_internal\`，不需要 Python 就能跑 VC。
  - **原始碼版**：資料夾裡有 `app\main.py`，需要 Python 3.10 以上。
- 不論哪一版，Claude 要用 `tools/vc_client.py` 與 SKILL 腳本，這台電腦都需要 Python 3.10 以上。

## 步驟 1：確認資料夾與 Python

1. 找出 VC 資料夾的完整路徑（使用者給的路徑，或含 `docs\CLAUDE_SETUP.md` 的資料夾）。確認裡面有 `Video Console.exe` 或 `app\main.py`。
   - 資料夾不要放在 `C:\Program Files`，VC 要在旁邊建立 `data\` 資料夾。若放在那裡，請使用者搬到「文件」或其他可寫入的位置。
2. 執行 `python --version`（或 `py -3 --version`）。沒有或低於 3.10 時，請使用者從 https://www.python.org/downloads/windows/ 安裝，安裝時勾選「Add python.exe to PATH」，裝完重開 Claude Code。

## 步驟 2：啟動並檢查

```bash
python "<VC 資料夾>/tools/vc_client.py" status
```

這個指令會在 VC 沒開時用 `--no-window` 背景啟動它，然後列出：

- VC 版本與連接埠
- `ffmpeg`（必要）、`video2x`（選用，放大影片）、`comfyui`（選用，AI 生成）的狀態
- 語音轉字幕引擎的檢查結果（最多約 3 分鐘）
- 專案數

結束代碼 0 代表必要元件都就緒。60 秒內沒啟動時，讀 `<VC 資料夾>/data/logs/app.log` 找原因。

執行檔版第一次啟動時 Windows 可能跳出「Windows 已保護您的電腦」，請使用者按「其他資訊」再按「仍要執行」，或先手動雙擊 `Video Console.exe` 一次。

## 步驟 3：ffmpeg（必要）

`ffmpeg` 顯示「缺少」時，二選一：

- 請使用者打開 VC，在「設定」頁的「依賴元件」按「一鍵安裝」。
- 經使用者同意後由 Claude 呼叫：`POST /api/system/deps/install`，本文 `{"id": "ffmpeg"}`，再用 `GET /api/system/deps` 確認 `ready: true`（下載大小見回應的 `sizeMB`，裝在 `data/tools/`）。

VC 也會自動找系統 PATH、imageio-ffmpeg 套件、WinGet 與 Scoop 安裝位置的 ffmpeg，已經裝過就不用再裝。

## 步驟 4：語音轉字幕（選用，剪訪談影片需要）

語音轉字幕用的是另一個專案 **Speech-to-subtitle**，目前沒有公開下載，需要從原本的電腦整個資料夾複製過來。

需求：NVIDIA 顯示卡（辨識只跑 CUDA，沒有 NVIDIA 顯示卡無法使用）、Python 3.12。

1. 把 `Speech-to-subtitle` 資料夾放在 VC 資料夾的**同一層**，例如 `D:\Tools\Video Console` 與 `D:\Tools\Speech-to-subtitle`。放別處也可以，之後在 VC「引擎」頁「語音轉字幕」分頁填路徑。
2. 執行該資料夾裡的 `安裝依賴.bat`。它會：把虛擬環境重新指到這台電腦的 Python 3.12、用 pip 安裝套件（有 NVIDIA 顯示卡時加裝 GPU 套件）、可用 winget 安裝 ffmpeg、下載 faster-whisper large-v3 模型（約 3 GB）。
3. 選用：MFA 精準對齊（字幕時間更準）。需要 Miniforge，裝在 `%USERPROFILE%\miniforge3`，然後：

   ```bash
   mamba create -n mfa -c conda-forge montreal-forced-aligner
   ```

   ```bash
   mamba run -n mfa mfa model download acoustic mandarin_mfa
   ```

   ```bash
   mamba run -n mfa mfa model download dictionary mandarin_taiwan_mfa
   ```

4. 再跑一次步驟 2 的 `status`，語音轉字幕要顯示「就緒」（cuda 與 model 都 OK；mfa 沒裝時顯示 NG 不影響使用）。

## 步驟 5：讓 Claude 記得 VC 在哪裡

1. 設定使用者環境變數 `VC_ROOT`，SKILL 腳本與 `vc_client.py` 會用它找 VC：

   ```bash
   setx VC_ROOT "<VC 資料夾>"
   ```

   設定後要重開 Claude Code 才會生效。
2. 詢問使用者是否要把下面這段加進 `%USERPROFILE%\.claude\CLAUDE.md`（使用者同意才寫）：

   ```markdown
   ## Video Console
   本機影片剪輯工具在 <VC 資料夾>。要剪片、轉字幕、輸出影片時，讀 <VC 資料夾>\docs\API.md，
   用 <VC 資料夾>\tools\vc_client.py 操作（python tools/vc_client.py status 檢查狀態）。
   ```

## 步驟 6：安裝 SKILL（選用）

`<VC 資料夾>\claude\skills\` 裡的每個資料夾是一個 Claude Code SKILL，複製到 `%USERPROFILE%\.claude\skills\` 即可使用：

| SKILL | 用途 |
|---|---|
| `interview-video-edit` | 指定一個訪談影片資料夾，自動轉 1080p、每位受訪者開一個 VC 專案、語音轉字幕、依規則標記或剪掉採訪者提問與長停頓、修字幕、輸出 |
| `script-to-vc-storyboard` | 讀一份腳本（.docx、.txt、.md），由 Claude 設計景別、運鏡、字幕切分與每鏡秒數，產出 VC「匯入劇本」一列一鏡、內容不被改動的 Word 分鏡表，並用 VC 原始碼離線自檢 |

```bash
cp -r "<VC 資料夾>/claude/skills/interview-video-edit" ~/.claude/skills/
```

```bash
cp -r "<VC 資料夾>/claude/skills/script-to-vc-storyboard" ~/.claude/skills/
```

`interview-video-edit` 需要步驟 3 的 ffmpeg 與步驟 4 的語音轉字幕；轉檔用 NVIDIA 硬體編碼（h264_nvenc），沒有 NVIDIA 顯示卡時改用 libx264，速度較慢。

`script-to-vc-storyboard` 需要 python-docx；自檢要讀 VC 原始碼（`app/vc/script.py`），只有執行檔版時無法自檢，改成實際匯入 VC 目視確認。

## 步驟 7：連線測試

用一個暫時的專案確認讀寫都正常，測完移到垃圾桶：

```python
import sys; sys.path.insert(0, r"<VC 資料夾>\tools")
from vc_client import VC
vc = VC()
p = vc.create_project("Claude 連線測試")
vc.ops(p["id"], [{"op": "shot.update", "id": p["shots"][0]["id"], "patch": {"color": "#6ea8ff", "notes": "測試"}}])
assert vc.project(p["id"])["shots"][0]["color"] == "#6ea8ff"
vc._request("DELETE", f"/api/projects/{p['id']}")
print("OK")
```

## 步驟 8：回報使用者

用一段話告訴使用者：VC 路徑與版本、哪些元件就緒、哪些沒裝（以及沒裝會影響什麼）、是否已安裝 SKILL、是否已寫入 CLAUDE.md、`VC_ROOT` 要重開 Claude Code 才生效。

## 操作 VC 時的守則

- 只透過 API 修改專案，不要直接改 `data/projects/<pid>/project.json`（伺服器有快取，直接改會被覆蓋）。
- 刪除使用者既有的專案、清空垃圾桶、清除專案檔案（purge）之前先問使用者。
- 會改變鏡頭位置或長度的修改用 `{"op": "linked", "ops": [...]}` 包起來，字幕才會跟著移動。
- 每次 `ops` 呼叫是使用者介面上的一個復原步驟，相關的修改放在同一次呼叫裡，使用者按一次 Ctrl+Z 就能整批收回。
- 在時間軸上分割、裁剪、定格、分離原聲、倒放、調整音量、加關鍵幀時，先用 `vc.timeline(pid)` 找片段位置，再用 `vc.edit(pid, 指令, ...)`（`docs/API.md` 3.7 節），結果和使用者按快捷鍵相同，不必自己組 op。
- 長工作（匯入 4K 檔、語音轉字幕、輸出）可能要數十分鐘，用 `vc.wait(job_id)` 等待；4K 素材先轉成 1080p 再匯入會快很多。

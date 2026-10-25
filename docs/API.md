# Video Console 本機 API 說明

給程式與 AI 助理（例如 Claude Code）直接操作 Video Console（以下簡稱 VC）用。網頁介面做得到的事，幾乎都能透過這組 API 完成：建專案、匯入影片、分割與修剪、語音轉字幕、改字幕、標色、匯出。

配套檔案：

- `tools/vc_client.py`：Python 用戶端（只用標準庫），本文的範例都用它。
- `docs/CLAUDE_SETUP.md`：新電腦交給 Claude 設置的步驟。
- `claude/skills/interview-video-edit/`：用這組 API 剪訪談影片的 Claude SKILL。

## 1. 連線

| 項目 | 說明 |
|---|---|
| 位址 | 只聽 `127.0.0.1`，連接埠 8790；被占用時往後找到 8799 |
| 目前連接埠 | `data/run/server.json` 的 `port` 欄位（VC 執行中才有） |
| 健康檢查 | `GET /api/ping` → `{"ok": true, "instance": "...", "version": "1.6.0", "build": "1.6.0+...", "running": 0, "tray": false}`（`build` 是執行中的程式碼版本，`running` 是執行中的工作數，`tray` 表示有沒有顯示托盤圖示） |
| 必要標頭 | POST、PUT、DELETE 一律加 `X-VC: 1`，否則 403 |
| 主機檢查 | `Host` 必須是 `127.0.0.1:<port>` 或 `localhost:<port>`，跨站請求一律 403 |
| 資料格式 | JSON 本文，UTF-8；上傳檔案用原始位元組本文（不是 multipart） |
| 錯誤格式 | `{"error": "中文訊息", "status": 409}`，HTTP 狀態碼同 `status` |

### 1.1 啟動方式

```bash
pythonw app/main.py --no-window        # 不開視窗，只跑伺服器
python app/main.py --stop              # 關閉正在執行的 VC
```

其他參數：`--port <n>` 指定起始連接埠、`--data <資料夾>` 指定資料位置（也可用環境變數 `VC_DATA_DIR`）、`--verbose` 詳細日誌。日誌在 `data/logs/app.log`。

打包版（`Video Console.exe`）一樣接受這些參數。

### 1.2 自動關閉與保活

用 `--no-window` 啟動的 VC 沒有托盤圖示，沒有任何視窗連著、也沒有工作在跑時，120 秒後自動關閉。程式操作時要保持一條 `GET /api/events`（SSE）連線，VC 會把它當成開著的視窗。`tools/vc_client.py` 會在背景自動做這件事，斷線也會重連。

從桌面開啟的 VC 會顯示系統托盤圖示，關閉視窗後留在托盤，不會自動關閉，要從托盤右鍵選單或 `POST /api/shutdown` 結束。使用者之後從桌面開啟背景那一份時，啟動程式會呼叫 `POST /api/tray` 補上托盤圖示。

啟動時若發現執行中的那一份跑的是更新前的程式碼（`/api/ping` 的 `build` 不同），啟動程式會先關掉它再重新啟動。它還有工作在執行時，從桌面開啟會先詢問，`--no-window` 啟動則保留它不重啟。

`tools/vc_client.py` 的 `VC()` 也會比對：用 VC 資料夾裡的原始碼算出 `build`（執行檔版讀打包時存下的 `_internal/vc/BUILD`），和執行中那一份不同時，用 `--no-window` 再啟動一次讓啟動程式換掉它，舊的那一份有托盤圖示時換好後補上。被保留時（有工作在執行）在 stderr 印出提示並沿用舊的那一份。這一版之前打包的執行檔沒有 `BUILD`，無法比對，不檢查。

### 1.3 id 規則

所有 id 必須符合 `^[a-z]{1,3}_[a-z0-9]{6,24}$`。VC 自己用的前綴：專案 `p_`、鏡頭 `s_`、版本 `t_`、字幕 `c_`、聲音 `a_`、工作 `j_`。用 `shot.add`、`caption.add` 新增項目時由呼叫端產生 id，例如 `"s_" + 10 碼小寫英數`。

## 2. 專案資料結構

`GET /api/projects/{pid}` 回 `{"project": {...}}`，重點欄位：

| 欄位 | 內容 |
|---|---|
| `id`、`name`、`rev` | `rev` 每次修改加 1 |
| `settings` | `width`、`height`（自動對齊 8 的倍數）、`fps`（1~60，整數或 29.97；29.97 輸出成 30000/1001）、`aspect`、`fadeIn`、`fadeOut`、`captionStyle` |
| `shots` | 鏡頭陣列，順序就是時間軸順序 |
| `takes` | `{take_id: 版本}`，每支匯入或生成的影片、圖片都是一個版本。影片版本與素材可能有 `preview2`、`preview4`：1/2、1/4 解析度預覽檔的路徑（`media/thumbs/<id>_p2.mp4`），由 `preview-files` 產生，只供預覽播放，輸出一律用原檔 |
| `captions` | 字幕陣列 `{id, start, end, text, position, speaker, style, x, y, shot, title}`，時間是序列上的秒數。`title: true` 是標題字卡（GR-10）：照樣燒進輸出，但不寫進 SRT。`shot` 是對應的鏡頭，同一鏡頭可以對應多句。設了 `x`、`y`（拖曳過）的字幕在同時出現又互相蓋到時，輸出與預覽會依開始時間把後出現的往畫面中央方向移開 |
| `cast` | 人物陣列 `{id, name, voice, speed, volume, engine, style, model, look, lookRef}`。`look`（最多 1000 字）是外觀描述：生圖、生影片時，鏡頭描述寫到這個名字（`name` 出現在描述中），就在描述後面加一行「名字：外觀」；鏡頭自己的生圖提示詞照原樣送出。`lookRef` 是外觀參考圖的版本或素材 id，只在介面上供一鍵套用成鏡頭參考圖，被刪除時清成 `null`（可復原） |
| `audio` | 音樂、音效、配音。`track` 是所在音軌的代號：同代號的片段排在同一條音軌，沒有 `track` 的片段自成一條；`track` 為 `voice` 或帶 `captionId` 的片段在配音軌。介面拖曳與上傳會避開同軌重疊，`audio.update` 直接改 `track` 不檢查。`fixed: true` 時連動不移動它；`speed`（0.25~4）只出現在變速鏡頭分離出的原聲，時間軸長度是 `(out - in) / speed` |
| `overlays`、`layers` | 上層畫面與軌道。上層畫面的 `x`、`y`（-4~4）是左上角佔畫面寬、高的比例，`w`（0.05~4）是寬度佔畫面寬的比例，高度照素材比例；超出畫面的部分被裁掉（填滿就是 `w` 大於 1）。`chroma` 是去背設定 `{on, color, similarity, blend}`：`color` 是 `#rrggbb`，`similarity` 0.01~1、`blend` 0~1（預設 `#00ff00`、0.15、0.1），`on: false` 時保留設定但不去背 |
| `adjustments` | 調整圖層陣列 `{id, name, start, length, hidden, grade}`，時間是序列上的秒數；格式見下方 |
| `tracks` | 軌道開關，例如 `adjustHidden: true` 隱藏整條調整圖層軌（預覽與輸出都不套用）；`musicOrder` 是音樂軌由上到下的代號（片段的 `track`，沒有 `track` 的用片段 id），沒列到的排在後面，只影響顯示 |
| `markers` | 時間軸標記陣列 `{id, start, color, name}`，`start` 是序列秒數；color 可用 `blue`、`red`、`orange`、`yellow`、`green`、`purple` |
| `review` | 整體的客戶意見，格式同鏡頭的 `review`；客戶寫的整體意見與已刪除鏡頭的意見放在這裡，用 `project.update` 的 `patch.review` 整組取代 |
| `media` | 素材庫。`tags` 是標籤陣列（每個最多 30 字、每個素材最多 20 個，重複的只留一個），用 `media.update` 的 `patch.tags` 整組取代 |

鏡頭常用欄位：

| 欄位 | 說明 |
|---|---|
| `name` | 鏡頭名稱 |
| `activeVideo` / `activeImage` | 使用中的版本 id |
| `trimIn` / `trimOut` | 入點／出點，原始影片的秒數；`trimOut: null` 代表播到結尾 |
| `inSequence` | `false` 時預覽與匯出都跳過 |
| `transition` | `{"type": "cut", "duration": 0.5}`；type 可用 `cut`、`crossfade`、`fade_black`、`fade_white`、`wipe_left`、`slide_left`，長度 0.1~5 秒 |
| `color` | 標籤顏色，時間軸片段整段染色；見下表 |
| `notes` | 備註（4000 字內），滑鼠停在片段上會顯示 |
| `review` | 客戶審稿意見陣列 `{by, at, text}`（SB-13）：留言者（60 字內）、留言時間（ISO 字串）、意見（2000 字內）；最多 200 則，沒有文字的略過。由 `POST /review` 加入，用 `shot.update` 的 `patch.review` 整組取代 |
| `duration` | 只有圖片或空白鏡頭時使用的長度 |
| `speed` | 影片播放速度 0.25~4（預設 1），聲音一起變速；時間軸上的長度是 `(trimOut - trimIn) / speed` |
| `disabled` | `true` 時保留位置，預覽與輸出都是黑畫面加靜音 |
| `audioDetached` | `true` 時鏡頭本身無聲，原聲改由音訊軌上 `shotId` 指向它的片段播放（見 3.7 的 `detach`） |
| `parked` | 鏡頭移出序列時收起的項目 `{"captions", "audio", "overlays", "adjustments"}`，時間從鏡頭開頭算；放回序列時由連動放回（見 3.2） |
| `grade` | 只套在這個鏡頭畫面的調色，格式同下方調整圖層的 `grade`，調整圖層再疊在上面；只對影片、圖片鏡頭有效 |
| `masks` | 模糊或馬賽克框，最多 8 個：`{x, y, w, h, mode, strength}`。`x`、`y`、`w`、`h` 是佔畫面寬、高的比例（`w`、`h` 0.02~1，框被夾在畫面內），`mode` 是 `blur` 或 `mosaic`，`strength` 0.1~1 控制模糊程度或格子大小。框固定在畫面上，不蓋上層畫面與字幕 |

停用片段在各軌用的欄位：鏡頭、字幕、上方畫面是 `disabled`，音訊是 `muted`，調整圖層是 `hidden`。

標籤顏色（介面上的 7 個選項）：

| 值 | 介面名稱 |
|---|---|
| `null` | 無顏色 |
| `#ff6b6b` | 紅色 |
| `#f5b14c` | 橘色 |
| `#c6f24e` | 萊姆綠 |
| `#4fd18b` | 綠色 |
| `#6ea8ff` | 藍色 |
| `#b28cff` | 紫色 |

調整圖層的 `grade`（缺的欄位用中性值補上，超出範圍會被夾住）：

| 欄位 | 範圍 | 中性值 | 說明 |
|---|---|---|---|
| `global`、`shadows`、`midtones`、`highlights` | 物件 | | 四個色輪，各有下面五個欄位 |
| 　`r`、`g`、`b` | -1~1 | 0 | 色輪色點的顏色偏移 |
| 　`sat` | 0~2 | 1 | 飽和度 |
| 　`bright` | -1~1 | 0 | 亮度 |
| `temp` | 2500~10000 | 5000 | 色溫，越高越暖 |
| `tint` | -100~100 | 0 | 色調，正值偏洋紅、負值偏綠 |
| `hue` | -180~180 | 0 | 色相旋轉（度） |
| `mix` | 0~1 | 1 | 混合比例 |

關鍵幀 `keyframes`：`[{"t": 0, "opacity": 0}, {"t": 2.5, "opacity": 1}]`，`t` 是片段內的秒數（取到 0.001 秒），最多 200 個，同一個 `t` 以後給的為準。兩個關鍵幀之間平順變化，第一個之前、最後一個之後維持端點的值。某個欄位只要有關鍵幀，就以關鍵幀為準，片段本身的那個欄位不再使用；沒有關鍵幀的欄位照舊。

| 片段 | 可加關鍵幀的欄位 |
|---|---|
| 音訊 `audio` | `volume`（0~4，1 是原音量） |
| 上方畫面 `overlays` | `x`、`y`、`w`、`opacity`、`volume`（圖片沒有 `volume`） |
| 調整圖層 `adjustments` | `grade`（整組調色，格式同上表） |

鏡頭與字幕不能加關鍵幀：要動的畫面放上方軌；鏡頭可以有自己的 `grade`，但要漸變的調色用調整圖層。

鏡頭在序列上的位置：依 `shots` 順序把 `inSequence` 的鏡頭接起來，每段長度是 `trimOut - trimIn`（影片）或 `duration`（圖片），有轉場時與下一段重疊轉場長度。

## 3. 修改專案：ops

```
POST /api/projects/{pid}/ops
{"ops": [...], "label": "分割片段", "undoable": true}
→ {"rev": 12, "changed": [...]}
```

- 一次呼叫是一個復原步驟（介面上的 Ctrl+Z 會整批收回）。
- 回應不含整個專案，要最新內容請再 `GET /api/projects/{pid}`。
- 一批最多 2000 個 op。
- 其他本文欄位：`undoable: false` 不進復原堆疊；`coalesce`（64 字內字串，短時間內相同字串的連續修改合併成一個復原步驟，例如連續打字）；`batch`（64 字內字串，原樣附在這次修改的 `project` 事件上，供呼叫端辨認自己送出的修改）。

### 3.1 op 一覽

| op | 參數 |
|---|---|
| `shot.add` | `{"shot": {"id": "s_...", ...}, "index": 3}` |
| `shot.update` | `{"id": "s_...", "patch": {...}}` |
| `shot.delete` | `{"id": "s_..."}` |
| `shot.move` | `{"id": "s_...", "index": 0}` |
| `caption.add` | `{"item": {"id": "c_...", "start": 1.2, "end": 3.4, "text": "..."}}` |
| `caption.update` | `{"id": "c_...", "patch": {"text": "...", "start": 1.0}}` |
| `caption.delete` | `{"id": "c_..."}` |
| `audio.*`、`overlay.*`、`cast.*` | 同 caption 的 add／update／delete 格式 |
| `adjust.add` | `{"item": {"id": "adj_...", "start": 0, "length": 6, "grade": {"temp": 6500}}}` |
| `adjust.update` | `{"id": "adj_...", "patch": {"start": 1.5, "grade": {...}}}`；`grade` 整組取代，沒給的欄位回到中性值 |
| `adjust.delete` | `{"id": "adj_..."}` |
| `marker.add`、`marker.update`、`marker.delete` | 標記，格式同 caption |
| `item.derive` | `{"id": "m_...", "patch": {"wave": "media/thumbs/m_..._wave.json", "alphaCodec": "vp9"}}`：重新讀檔得到的版本或素材資料（波形檔、縮圖 `thumb`、膠卷縮圖 `strip`、透明 webm 的編碼 `vp8`／`vp9`、預覽檔 `preview2`／`preview4`），開啟專案時的補齊流程使用，一般修改不需要 |
| `item.relink` | `{"id": "m_...", "patch": {"file": "media/...", "duration": 8.0}, "unset": ["wave"]}`：把素材或版本換成另一個檔（重新連結），用到它的鏡頭與片段不動。由 `upload?target=relink` 的工作送出，一般不直接用 |
| `captionStyle.*`、`take.*`、`media.*`、`layer.*` | 樣式庫、版本、素材、軌道 |
| `project.update` | `{"patch": {"name", "notes", "settings", "style", "tracks", "review"}}` |
| `batch` | `{"op": "batch", "ops": [...]}`：多個 op 包成一組 |
| `linked` | `{"op": "linked", "ops": [...]}`：鏡頭變動時字幕、配音、上層畫面跟著移動；加 `"shift": false` 只收起、放回，不平移 |

字幕文字上限 500 字。

各種 `*.update` 可以多帶 `"unset": ["欄位", ...]` 把欄位整個拿掉（例如 `{"op": "audio.update", "id": "a_...", "patch": {}, "unset": ["keyframes"]}`）；和設成 `null` 不同，拿掉後回到「沒有這個欄位」的預設行為。

### 3.2 連動（linked）

介面上「連動上下軌」開著時，鏡頭的修剪、移動、刪除都用 `linked` 包起來送出。後端會依鏡頭前後的位置變化，把字幕、配音、音樂、上層畫面、調整圖層一起平移（調整圖層只移開始時間，長度不變）；刪除鏡頭時，整句落在該鏡頭範圍內的字幕（與它的配音）一起刪除。把鏡頭移出序列（`inSequence: false`）時，整個落在它上面的項目收進鏡頭的 `parked`，放回序列時回到鏡頭的新位置。同一批 op 自己改過的項目（開始時間有變或新加入）與 `fixed` 的音訊不移動。

介面上連動關閉、但有鏡頭收著項目時，鏡頭編輯會用 `{"op": "linked", "shift": false}` 送出，只負責放回。生成工作（`POST /api/jobs` 的 `payload.link`）與匯入劇本（`link`）預設連動，送 `false` 時不移動其他軌。

程式操作時，凡是會改變鏡頭位置或長度的 op（trim、delete、move、add），都建議用 `linked` 包起來，否則字幕會停在原本的時間。

### 3.3 範例：在原始影片第 12.5 秒分割鏡頭

依時間軸位置分割時，直接用 3.7 的 `vc.edit(pid, "split", t=...)` 比較簡單，它還會給後段每個版本一份複本（同一個檔案），兩段可以各自換版本；下面的手動做法兩段共用版本。以下是依原始影片秒數自己組 op 的做法，分割就是「前段改出點」加上「複製一份當後段」：

```python
shot = next(s for s in project["shots"] if s["id"] == sid)
index = project["shots"].index(shot)
tail = {**shot, "id": new_shot_id(), "trimIn": 12.5, "trimOut": shot["trimOut"]}
vc.ops(pid, [{"op": "linked", "ops": [
    {"op": "shot.update", "id": sid, "patch": {"trimOut": 12.5, "transition": {"type": "cut", "duration": 0.5}}},
    {"op": "shot.add", "shot": tail, "index": index + 1},
]}], label="分割片段")
```

### 3.4 範例：標色加備註（不刪）

```python
vc.ops(pid, [{"op": "shot.update", "id": sid,
              "patch": {"color": "#f5b14c", "notes": "停頓 3.2 秒，評估是否刪除"}}], label="標記")
```

### 3.5 範例：刪除鏡頭（字幕跟著刪）

```python
vc.ops(pid, [{"op": "linked", "ops": [{"op": "shot.delete", "id": sid}]}], label="刪除鏡頭")
```

### 3.6 復原與版本紀錄

| 端點 | 說明 |
|---|---|
| `POST /api/projects/{pid}/undo`、`/redo` | 等同 Ctrl+Z／Ctrl+Shift+Z |
| `GET /api/projects/{pid}/history` | 伺服器端快照列表 |
| `POST /api/projects/{pid}/history/{rev}/restore` | 還原到某個快照（可再復原） |

### 3.7 剪輯指令：/timeline 與 /edit

剪輯頁的快捷鍵功能（分割、裁剪、定格、分離原聲、倒放、音量、關鍵幀、修剪模式）本身就是送這些指令，按鍵與 Claude 共用 `vc/edits.py` 這一份實作。指令用時間軸上的秒數指定位置，伺服器算出 op 再送出：鏡頭變動預設用 `linked` 包起來（字幕、音訊、上方畫面跟著移動），一次呼叫是一個復原步驟。

先讀時間軸，知道每個片段在哪裡：

```
GET /api/projects/{pid}/timeline
→ {"duration": 42.5,
   "clips": [{"shotId", "index", "name", "source", "takeId", "start", "length", "in", "out", "speed", "disabled", "audioDetached"}],
   "items": [{"type", "id", "start", "length", "name", "keyframes"}],
   "markers": [{"id", "start", "color", "name"}]}
```

- `clips`：畫面軌依序排列；`start`、`length` 是序列秒數，`in`、`out` 是原始影片秒數，`index` 是在 `shots` 陣列中的位置；`source` 是 `video`、`image` 或 `empty`（空白鏡頭）。
- `items`：其他軌的片段，`type` 是 `overlay`、`audio`、`caption`、`adjust`；`keyframes` 是關鍵幀數量。

再送指令：

```
POST /api/projects/{pid}/edit
{"cmd": "split", "t": 12.5}
→ {"label": "分割鏡頭", "changed": [...], "rev": 13, "id": "s_..."}
```

共用參數：`t` 是序列秒數；`type` 是片段種類 `shot`（預設）、`overlay`、`audio`、`caption`、`adjust`；`id` 是片段 id。沒給 `id` 時取 `t` 下方的片段（鏡頭取畫面軌，其他種類取第一個蓋到 `t` 的片段）。可選 `link: false`（剪輯頁「連動」關閉時送）讓鏡頭變動不帶動其他軌，一起送出的工作也帶 `payload.link: false`；可選 `coalesce`，用法同 3 節的本文欄位，只拿來合併改同一批欄位的連續指令（合併後復原只還原第一次修改前的那些欄位，剪輯頁連按音量鍵時依「整段」或「區域」分開）。

| cmd | 參數 | 作用 | 回傳的 `id` |
|---|---|---|---|
| `split` | `t`，可選 `type`、`id` | 在 `t` 切開片段；關鍵幀分到兩段，切點補一個關鍵幀保持值不變 | 後段 |
| `splitAll` | `t` | 把 `t` 經過的每一軌一起切開 | |
| `trim` | `t`、`side`（`start` 或 `end`），可選 `type`、`id` | `start` 刪掉 `t` 之前的部分，`end` 刪掉之後的部分；畫面軌後面的鏡頭往前補 | |
| `freeze` | `t` | 在 `t` 切開影片鏡頭，中間插 2 秒靜止畫面，並送出 `lastframe` 工作擷取那一格（回傳的 `job`）；`t` 在鏡頭頭尾時定格加在前面或後面 | 定格鏡頭 |
| `detach` | `id` 或 `t` | 把鏡頭原聲拆成音訊片段，鏡頭改成無聲 | 新的音訊片段 |
| `restore` | `id`（鏡頭或它的原聲片段） | 刪掉原聲片段，鏡頭恢復有聲 | |
| `reverse` | `id` 或 `t` | 第一次送出 `reverse` 工作產生倒放影片（不改專案，回傳 `job`）；已有倒放版本時在正放、倒放之間切換，修剪範圍跟著鏡像 | |
| `roll` | `id`（剪接點前的鏡頭）、`by` | 把這個鏡頭與下一個鏡頭之間的剪接點移動 `by` 秒（正數往後）：前一個鏡頭的出點、後一個的入點一起改，總長度與其他鏡頭不變；素材或鏡頭長度不夠時移到能移的最遠處，回傳實際的 `by` | |
| `slip` | `id`、`by` | 影片鏡頭的入點、出點一起移動 `by` 秒（正數換成較晚的畫面），位置與長度不變；不連動字幕與上層片段 | |
| `slide` | `id`、`by` | 鏡頭內容不變，跟前後兩個剪接點一起移動 `by` 秒：前一個鏡頭變長、後一個變短（或反過來） | |
| `volume` | `type`、`id`、`db`，可選 `in`、`out`、`t` | 音量加減 `db`（例如 1 或 -1）；給 `in`、`out`（序列秒數）時只調區域內，兩端自動加關鍵幀，前後各 0.1 秒漸變。另回 `level`（調整後的音量：有區域時是區域起點的值；片段有音量關鍵幀時是 `t` 處的值，沒給 `t` 用片段起點）與 `partial`（是否只調了區域內） | |
| `keyframe` | `type`、`id`、`t`，可選 `values` | 在 `t` 記下片段目前的值當關鍵幀；`values` 例如 `{"opacity": 0}` 覆蓋要記的值 | |

限制（違反時回 400 與中文說明）：

- `volume` 只能用在 `audio` 與上方影片（`overlay`）。鏡頭原聲要先 `detach`；圖片沒有聲音。
- `keyframe` 只能用在 `audio`、`overlay`、`adjust`，`values` 只能用 2 節關鍵幀表裡的欄位。
- 變速中的鏡頭 `detach` 後，音訊片段帶 `speed`（來源秒數／時間軸秒數），長度與畫面一致；分離期間鏡頭速度鎖定。
- 已分離原聲的鏡頭可 `freeze`：跨過定格點的原聲在該處切開，後段與之後同素材的原聲一律後移定格長度（不看連動開關）。
- 已分離原聲的鏡頭可 `reverse`：只換畫面，原聲照常順向播放；`restore` 以素材檔比對，倒放版本視同原始素材。
- `freeze`、`reverse` 只能用在影片鏡頭。
- `roll`、`slip`、`slide` 只作用在畫面軌，`by` 以時間軸秒數計（變速鏡頭的素材移動量是 `by` 乘速度）。`roll` 不能用在最後一個鏡頭，`slip` 只能用在影片鏡頭，`slide` 要前後都有鏡頭；已經移到底時回 400「已經到底了」。分離出來的原聲不跟著移動。
- 切點離片段邊緣太近（0.1 秒內，圖片 0.2 秒內）時不能 `split`、`trim`。

範例：把第 20 到 25 秒的配樂降 6dB，再讓上方圖片在 29 到 30 秒淡出。`in` 是 Python 保留字，要用 `**{...}` 傳。

```python
tl = vc.timeline(pid)
music = next(i for i in tl["items"] if i["type"] == "audio" and i["name"] == "配樂")
vc.edit(pid, "volume", type="audio", id=music["id"], db=-6, **{"in": 20, "out": 25})
logo = next(i for i in tl["items"] if i["type"] == "overlay")
vc.edit(pid, "keyframe", type="overlay", id=logo["id"], t=29)
vc.edit(pid, "keyframe", type="overlay", id=logo["id"], t=30, values={"opacity": 0})
```

## 4. 匯入檔案

```
POST /api/projects/{pid}/upload?name=<檔名>&target=shot&shot=<shot_id>
本文：檔案原始位元組，需帶 Content-Length
→ {"job": {...}}   # ingest 工作，用 GET /api/jobs/{jid} 等它完成
```

| 參數 | 說明 |
|---|---|
| `name` | 必填，副檔名決定類型 |
| `target` | `library`（素材庫，預設）、`shot`（成為該鏡頭使用中的版本）或 `relink`（重新連結） |
| `shot` | `target=shot` 時指定鏡頭 |
| `media` | `target=relink` 時指定要換檔的素材。新檔要和原素材同類（圖片、影片、聲音），不同類回 400；素材代號不變，從同一個檔匯入的鏡頭版本一起換，可以復原，舊檔留在專案資料夾 |
| `track`、`start`、`layer` | 放到時間軸的音軌或上層軌，`start` 為序列秒數 |
| `audioTrack` | 聲音檔放到時間軸時指定音軌：`voice`（配音軌）或某條音軌的代號（該軌片段的 `track`）。那段時間該軌已有聲音就改成自成一條音軌；代號格式不對回 400「音軌代號不正確」 |

支援格式：影片 .mp4 .webm .mov .mkv .m4v .avi；圖片 .png .jpg .jpeg .webp .bmp .gif；聲音 .mp3 .wav .m4a .aac .flac .ogg .opus。聲音不能放進鏡頭。不是 h264／vp8／vp9／av1 的 .mp4、.m4v、.webm，以及其他容器，會自動轉成瀏覽器可播放的格式（4K 或高位元率素材建議先轉成 1080p，速度差很多）。

透明背景的 .webm（VP8／VP9 帶 alpha，例如 p5.js 逐格輸出後用 `ffmpeg -c:v libvpx-vp9 -pix_fmt yuva420p` 編碼）原檔保留，放在上層軌時輸出會保留透明，下層畫面透出；素材資料多一個 `alphaCodec` 欄位。放在畫面軌（鏡頭）時透明部分不生效。這一版之前匯入的透明 webm 要重新匯入才會被認出。

新專案會有一個空白鏡頭，把第一支影片 `target=shot` 上傳到它即可；多支影片時先 `shot.add` 新鏡頭再上傳。

`GET /api/projects/{pid}/missing` 回 `{"media": [素材 id, ...]}`：檔案已經不在專案資料夾裡的素材，可以用 `target=relink` 換檔。

## 5. 背景工作

| 端點 | 說明 |
|---|---|
| `POST /api/jobs` | `{"kind", "pid", "shotId", "payload"}` → `{"job": {...}}` |
| `POST /api/jobs/batch` | 一次送多個 |
| `GET /api/jobs?pid=<pid>` | 列出工作 |
| `GET /api/jobs/{jid}` | 狀態、進度 `progress`（0~1）、`message`、`error`、`result` |
| `GET /api/jobs/{jid}/preview` | 執行中的生成工作最新一張即時預覽圖（`previewRev` 每換一張加 1；沒有時 404） |
| `POST /api/jobs/{jid}/cancel`、`/retry`、`/top` | 取消、重試、移到最前。`/retry` 本文可帶 `{"newSeed": true}` 換一個新種子重做（工作沒有種子時 409） |
| `POST /api/jobs/{jid}/move` | `{"before": jid}` 把排隊中的工作移到另一個排隊中的工作前面，`before` 為 `null` 時排到最後；工作已開始或目標不在排隊中時 409。配音工作仍然先跑 |
| `DELETE /api/jobs/{jid}` | 移除已結束的工作 |
| `POST /api/jobs/clear`、`/cancel-all`、`/pause` | 清除已結束、全部取消、暫停佇列（`/pause` 本文 `{"paused": true}` 暫停、`false` 繼續，也會取消夜間排程） |
| `POST /api/jobs/schedule` | 夜間排程 `{"start": 秒, "stop": 秒}`（epoch 秒數，`stop` 選填）：佇列先暫停，到 `start` 開始跑，到 `stop` 再暫停，執行中的工作會做完；`start` 不在未來或 `stop` 不在 `start` 之後時 400；兩個都給 `null` 取消排程（佇列維持暫停）。只排一次，APP 要開著；佇列狀態（`GET /api/jobs` 的 `state` 與 SSE 的 `queue` 事件）多帶 `resumeAt`、`pauseAt`（epoch 秒數，沒有排程時 `null`） |

狀態：`queued`、`running`，結束時是 `done`、`failed`、`canceled`、`interrupted` 其中之一。

設定 `queue.autoRetry`（0~5，預設 0）：因網路錯誤失敗的工作自動重試幾次，每次間隔逐步拉長。

| kind | 用途 | payload |
|---|---|---|
| `ingest` | 匯入檔案 | 由 upload 端點建立，不要直接送 |
| `transcribe` | 語音轉字幕 | `{}`，需要 `shotId` |
| `export` | 輸出影片 | 見第 7 節 |
| `package` | 打包 ZIP | `{"includeVideos", "includeExport", "fullBackup"}` |
| `voice` | 文字轉配音 | 依配音引擎設定 |
| `image`、`video` | ComfyUI 或雲端生成 | 依工作流程設定。`image` 可帶 `chainVideo: true`（完成後接著生影片）與 `chainUpscale: true`（先用 ComfyUI 放大範本放大，放大後的圖片成為鏡頭圖片再生影片，只在 `chainVideo` 時有效） |
| `upscale`、`upscalevideo` | 放大 | 需要 video2x |
| `kenburns` | 圖片推拉 | |
| `lastframe` | 擷取一格畫面成圖片 | `{"source", "target", "targetShot", "at"}`：`at` 是原始影片秒數（不給時取最後一格）；`target` 是 `next`（下一個鏡頭，預設）、`self`（本鏡頭）或 `shot`（`targetShot` 指定的鏡頭，定格用） |
| `reverse` | 產生倒放影片，完成後換上並鏡像修剪範圍 | `{"source": 版本 id}`，不給時用鏡頭使用中的影片 |

同時執行數量：ComfyUI 與語音轉字幕共用 1 個、雲端服務 2 個、ffmpeg 類 2 個、輸出 1 個。

雲端生成：
- 供應商設定的 `price`（美元，0 表示不估算）是影片每秒或圖片每張的單價。有單價時工作的 `payload.cost` 是預估金額，`notes` 寫出預估費用，完成後記進本月累計（`GET /api/costs` 回 `{"month", "usd", "jobs", "byProvider"}`，`/api/bootstrap` 的 `costs` 與 SSE 的 `costs` 事件同一格式）。
- 雲端版本的 `source.requestKey` 是請求代號（供應商設定、提示詞、尺寸、種子、秒數、參考圖）。同一個專案再送出代號相同的請求時，複製那個版本的檔案成為新版本，不再呼叫雲端，`notes` 會註明。
- 影片比例和專案差超過 5% 時置中裁成專案比例（重新編碼，原檔不保留），`notes` 會註明原本的比例。
- 偵測模型（`POST /api/providers/{id}/models`）的影片欄位多帶 `durations`（每個模型可用的秒數）與 `ratios`（fal.ai 模型可用的比例）。Replicate 需要 Token；可靈、Runway、MiniMax、Stability 讀 GitHub 上 main 分支的 `app/vc/providers/model_lists.json`，讀不到時用內建清單，`message` 會說明。

風格庫（所有專案共用，存在 `data/style-library.json`）：`GET /api/styles` 回 `{"styles"}`，新的在前；`POST /api/styles {"name", "prompt", "negative", "pid", "item"}` 新增一筆，`pid` 與 `item`（版本或素材 id）選填，給了就複製那張圖當樣張，回 `{"style", "styles"}`；`DELETE /api/styles/{id}` 刪除；`GET /api/styles/{id}/picture` 取樣張（沒有時 404）。樣張只是外觀參考，生成時不會送出。

## 6. 語音轉字幕

```python
job = vc.job("transcribe", pid, shot_id)
vc.wait(job["id"])
```

- 辨識該鏡頭修剪範圍內的原聲，字幕直接加在鏡頭在時間軸上的位置。鏡頭必須在序列裡、影片要有聲音。
- 已有字幕時新字幕會另外加入，不會取代。整批可用一次 undo 收回。
- 引擎是另一個專案 Speech-to-subtitle（faster-whisper large-v3，選用 MFA 精準對齊）。設定在 `settings.stt`：`{"folder": "", "python": ""}`。留白時找 VC 同一層的 `Speech-to-subtitle` 資料夾，Python 用它 `pyvenv.cfg` 指向的直譯器。
- 自我檢查：`POST /api/stt/check`（最多 180 秒），回 `{"ok", "cuda", "model", "ffmpeg", "mfa", "opencc", "folder", "python"}`，`ok` 需要 CUDA 與模型都就緒。
- 取得 SRT：`GET /api/projects/{pid}/captions.srt`（UTF-8 含 BOM；不含標題字卡；沒有字幕時 409）。
- 匯入 SRT：`POST /api/projects/{pid}/captions.srt`，body 是 .srt 檔原始內容（最多 10 MB），回 `{"rev", "added", "voiced"}`。字幕放在檔案記載的時間（序列秒數）加上 `?offset=` 秒數（預設 0），位置 `bottom`、不綁鏡頭；`<...>` 與 `{\...}` 標記會拿掉，多行文字保留換行（每行去頭尾空白、略過空行），超過 500 字截斷。編碼依序判斷：有 BOM 的 UTF-16、UTF-8（BOM 可有可無）、其餘當 Big5。加 `?replace=1` 先刪掉專案現有字幕（標題字卡保留），這時 `?voices=` 決定綁在舊字幕上的配音：`delete`（預設，刪掉）、`keep`（留在配音軌當一般片段）、`regen`（刪掉，新字幕沿用時間重疊最多的舊字幕的說話者，方便重新配音）；`voiced` 是有說話者的新字幕 id。整批是一個 undo 步驟（標籤「匯入 SRT」）。檔案裡沒有可用字幕時回 400 且不動現有字幕。

## 7. 輸出

```python
path = vc.export(pid, quality="high", burnCaptions=False)
```

`POST /api/jobs` 的 `{"kind": "export", "pid": pid, "payload": {"options": {...}}}`，options：

| 欄位 | 值 |
|---|---|
| `quality` | `high`、`standard`、`draft` |
| `encoder` | `auto`、`libx264`、`h264_nvenc`、`h264_amf`、`h264_qsv` |
| `burnCaptions` | 字幕燒進畫面 |
| `captionSize` | 燒入字幕大小 16~160 px（預設 52） |
| `fadeIn`、`fadeOut` | 開頭淡入、結尾淡出秒數 |
| `clipAudio` | 保留影片片段本身的聲音 |
| `fit` | 畫面比例不同時 `contain`（完整顯示）或 `cover`（填滿） |
| `format` | `mp4`（預設）；`wav`（16-bit PCM）或 `mp3`（320k）只輸出混音後的聲音，不算畫面 |
| `size` | 輸出尺寸 `[寬, 高]`（16~7680，自動取偶數）。和專案比例差 1% 以內時整段直接用這個尺寸合成（放大過的素材不會先縮回專案尺寸）；比例不同時合成完再縮放，依 `fit` 加黑邊或裁切。省略或 `null` 用專案尺寸 |
| `maxrate` | 影片位元率上限（kbps，0 為不限）。x264 與 NVENC 在上限內維持畫質目標；AMF 與 Quick Sync 改用這個位元率的 VBR |
| `loudness` | 響度標準化目標 `-14` 或 `-16`（LUFS），`null`（預設）不調整。先量整段混音，再用單一增益調整，峰值不超過 -1 dBTP；其他值回 400 |

輸出視窗的平台預設組（YouTube、Shorts、Reels、LINE）就是換算成 `size` 與 `maxrate`，選擇記在設定 `export.preset`（`""`、`youtube`、`shorts`、`reels`、`line`）。沒選平台時的「輸出尺寸」記在 `export.resolution`：`""`（專案尺寸）、`1080`、`2160`（短邊像素，和專案同比例）。

首頁的「批次輸出」就是對每個勾選的專案各送一個 `export` 工作（`POST /api/jobs/batch`），輸出通道一次跑一個。

輸出前可先 `GET /api/projects/{pid}/export-check`，回傳 `{"duration", "empty", "missing", "disabled", "missingAudio", "missingOverlays", "audioPastEnd", "captionsPastEnd", "videoHidden"}`：`empty`、`missing`、`disabled` 是鏡頭清單的編號（沒有畫面、檔案不見、停用），`missingAudio`、`missingOverlays` 是不見的檔名，`...PastEnd` 是超出影片結尾、會被切掉的數量。只列出問題，不會擋下輸出。

有指定 `loudness` 時，工作的 `notes` 會寫出調整前後的響度（例如「響度從 -20.3 調整到 -14 LUFS」），峰值太高、loudnorm 改用壓縮時另外註明；混音幾乎無聲（低於 -70 LUFS）時不調整。

完成後 `job["result"]["file"]` 是輸出檔的完整路徑，位在匯出資料夾：設定 `export.folder`（`PUT /api/settings {"export": {"folder": "D:\\影片輸出"}}`），留白時是 `data/exports/`，所有專案共用，影片、音訊與打包 ZIP 都存在這裡。改用共用資料夾前輸出的舊檔留在專案資料夾，`file` 是相對路徑 `exports/...mp4`；`Path(專案資料夾) / file` 兩種都能用，專案資料夾在 `data/projects/<pid>/`。也可用 `GET /api/projects/{pid}/exports` 列出輸出檔。

## 8. 即時事件（SSE）

`GET /api/events`，每則格式 `data: {"type": "...", "data": {...}}`。第一則是 `hello`（`{"instance", "version", "build"}`；重新連線後 `build` 和先前不同，代表 VC 已換成新版，介面會提示重新整理），緩衝溢位時送 `resync`（重新抓全部狀態），閒置時送 `: ping` 註解行。

type：`system`、`deps`、`comfy`、`workflows`、`queue`、`job`、`jobs`、`notify`、`settings`、`project`、`projects`、`costs`。

## 9. 端點總表

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/ping` | 健康檢查 |
| GET | `/api/bootstrap` | 介面啟動所需的全部狀態 |
| GET | `/api/events` | SSE 事件串流 |
| GET | `/api/system` | 系統資訊，含記錄檔總大小 `logBytes` |
| GET | `/api/system/fonts` | 可用字型 |
| POST | `/api/system/ffmpeg` | 重新偵測 ffmpeg |
| POST | `/api/system/reveal` | 在檔案總管開啟資料夾 `{"what": ...}`：`data`、`logs`、`workflows`、`projects`（所有專案）、`exports`（匯出資料夾）。檔案總管視窗會移到最上層（已開著的資料夾約 1 秒後） |
| GET | `/api/system/deps` | 相依套件狀態 `{"items": [{id, required, ready, path, version, installedVersion, latest, update, task}]}`：`installedVersion` 只有用的是 `data/tools` 裡 APP 裝的那份才有值（否則 `null`），`latest` 是目前清單上的版本，`update` 為真代表可以更新 |
| POST | `/api/system/deps/check` | 讀 GitHub 儲存庫上的 `app/vc/deps.json`，版本較新的元件換成新清單；回傳同上，另加 `remark`（取不到或格式不對時的說明，正常時空字串） |
| POST | `/api/system/deps/install` | 安裝或更新相依套件 `{"id": ...}`；下載中斷時保留已下載的部分，下次從斷點接著下載，來源失敗時換下一個 |
| GET | `/api/system/release` | 比對 GitHub 上最新 Release `{"current", "latest", "newer", "url", "published", "remark"}`；結果保留 6 小時 |
| POST | `/api/system/release/check` | 同上，但不用保留的結果，重新查一次 |
| GET | `/api/system/debug-bundle?project=pid` | 除錯資料 ZIP：記錄檔、系統資訊、設定（ntfy 主題遮住）、金鑰只標示有沒有設定、工作清單；加 `project` 時附上該專案的 project.json（找不到 404） |
| POST | `/api/system/clear-logs` | 清除記錄檔（使用中的清空，其餘刪除），回 `{"freed", "system"}` |
| POST | `/api/system/client-error` | 頁面錯誤寫入 app.log `{"message", "stack", "view"}` |
| POST | `/api/system/pick-folder` | 開啟 Windows 選擇資料夾視窗，回 `{"path"}`（按取消時 `null`）；已有一個開著時 409，非 Windows 501 |
| POST | `/api/shutdown` | 關閉 VC |
| POST | `/api/tray` | 顯示系統托盤圖示，回傳 `{"tray": bool}`（從桌面啟動時由啟動程式呼叫） |
| GET／PUT | `/api/settings` | 設定。`notify.ntfy.when`：`all`（全部完成都通知）或 `failed`（只通知失敗）；`history.keep`：每個專案保留幾份版本紀錄（5~500，預設 50）；`glossary`：字幕用語表（最多 20000 字），一行一個正確寫法，可接「=錯字,錯字」，只在瀏覽器檢查，不影響輸出 |
| POST | `/api/settings/default-updates` | 回答「預設值已更新」提示：`{"apply": [項目, ...]}` 列出的換成新預設，其餘保留，回 `{"settings", "providers"}` |
| GET／PUT | `/api/secrets` | API 金鑰（只回是否已設定與遮罩後的值） |
| GET／POST | `/api/projects` | 專案列表／新增 `{"name", "settings", "starter"}` |
| POST | `/api/projects/import?name=x.zip` | 匯入打包的 ZIP（本文為檔案） |
| GET／DELETE | `/api/projects/{pid}` | 讀取／移到垃圾桶（有工作時 409）。DELETE 加 `?exports=1` 一併刪掉匯出資料夾裡這個專案輸出的檔，回 `{"removedExports", "exportsInUse"}`（被其他程式開著刪不掉的檔數），這些檔無法從垃圾桶還原 |
| POST | `/api/projects/{pid}/open` | 設為最近開啟的專案 |
| POST | `/api/projects/{pid}/duplicate` | 複製專案，回傳 `{"project": {...}}`，鏡頭與字幕代號和原專案相同 |
| POST | `/api/projects/{pid}/ops` | 修改（第 3 節） |
| GET | `/api/projects/{pid}/timeline` | 時間軸位置（3.7 節） |
| POST | `/api/projects/{pid}/edit` | 剪輯指令（3.7 節） |
| POST | `/api/projects/{pid}/undo`、`/redo` | 復原、重做 |
| GET | `/api/projects/{pid}/history` | 版本紀錄 |
| POST | `/api/projects/{pid}/history/{rev}/restore` | 還原版本 |
| POST | `/api/projects/{pid}/upload` | 上傳檔案（第 4 節） |
| POST | `/api/projects/{pid}/preview-files` | 製作低解析度預覽檔 `{"scale": 2}`（2 或 4）：在背景轉檔還沒有預覽檔的影片，回 `{"pending": 還要製作的數量}`；每完成一支就寫進版本或素材的 `preview2`／`preview4`（專案變更事件） |
| GET | `/api/projects/{pid}/file?path=...` | 讀取專案內的檔案；`path` 也可以是這個專案輸出到匯出資料夾的完整路徑 |
| GET | `/api/projects/{pid}/exports` | 輸出檔列表：匯出資料夾裡這個專案輸出的檔案，加上專案資料夾的舊檔，`path` 可直接給 `file?path=` |
| DELETE | `/api/projects/{pid}/exports/{name}` | 刪除輸出檔（`name` 是檔名，只能刪這個專案輸出的檔） |
| GET | `/api/projects/{pid}/export-check` | 輸出前檢查（第 7 節） |
| GET | `/api/projects/{pid}/captions.srt` | 字幕 SRT |
| POST | `/api/projects/{pid}/captions.srt` | 匯入 SRT 字幕（第 6 節） |
| GET | `/api/projects/{pid}/shots.csv` | 鏡頭清單 CSV |
| GET | `/api/projects/{pid}/storyboard` | 分鏡表 HTML |
| GET | `/api/projects/{pid}/review.html` | 客戶審稿網頁（SB-13）：序列中每個鏡頭的畫面（data: URI，單檔可離線開啟）、秒數、提示詞與字幕，以附件下載（檔名 `<專案>_審稿.html`）。客戶在頁面留言後下載 `{"vc": "review-comments", "version": 1, "project", "name", "by", "at", "note", "shots": [{"id", "no", "name", "text"}]}` 的 .json 檔 |
| POST | `/api/projects/{pid}/review` | 匯入審稿意見檔（body 是 .json 原始內容，最多 2 MB，可有 BOM）：鏡頭意見加到該鏡頭的 `review`，`note` 與已刪除鏡頭的意見加到專案的 `review`，整批一個 undo 步驟（標籤「匯入審稿意見：<留言者>」）。回 `{"rev", "by", "shots", "general", "missing", "duplicate"}`（`duplicate` 是留言者、時間、文字都相同而略過的則數）。不是意見檔、屬於別的專案、全部已匯入過或沒有任何意見時回 400 |
| POST | `/api/projects/{pid}/reveal` | 在檔案總管開啟專案資料夾 |
| GET | `/api/projects/{pid}/usage` | 檔案使用量 `{"used", "unused", "orphans"}`（位元組；`orphans` 是沒用到的檔案相對路徑，含過期的暫存與中斷存檔留下的檔） |
| POST | `/api/projects/{pid}/purge` | 刪除 `orphans` 並清空復原堆疊，回 `{"freed"}` |
| POST | `/api/projects/{pid}/script/apply` | 套用劇本 |
| POST | `/api/script/parse`、`/api/script/docx` | 解析劇本文字、Word 檔（套用時產生的字幕 `shot` 就是它來自的鏡頭）。parse 收 `{"text", "mode", "detail"}`，回 `{"shots", "mode", "notes"}`；`detail: true` 時，劇本已寫好分鏡的段落（有標題列的分鏡表、編號動作）照原文切：每個動作一鏡，台詞放到說話者出現的鏡頭，備註欄的運鏡指示另成一鏡；其餘段落拆成 2 到 5 個不同景別的鏡頭；秒數依節奏與旁白長度安排，每個鏡頭多帶 `beat`（第幾段）與 `pace`（`slow`、`medium`、`fast`），套用時這兩欄不會存進鏡頭 |
| GET／DELETE | `/api/trash` | 垃圾桶列表／清空 |
| POST | `/api/trash/{folder}/restore` | 從垃圾桶還原 |
| | `/api/jobs...` | 背景工作（第 5 節） |
| POST | `/api/stt/check` | 語音轉字幕自我檢查 |
| | `/api/comfy...`、`/api/workflows...` | ComfyUI 伺服器與工作流程 |
| | `/api/providers...`、`/api/llm/...` | 雲端生成服務、提示詞潤飾 |
| GET | `/api/costs` | 本月雲端預估費用（第 5 節） |
| | `/api/styles...` | 風格庫（第 5 節） |
| | `/api/voice/...` | 配音引擎 |
| POST | `/api/notify/test` | 測試完成通知 |

## 10. 注意事項

- VC 會把專案存成 `data/projects/<pid>/project.json`，請只透過 API 修改，不要直接改檔（伺服器有記憶體快取，直接改會被覆蓋）。每次修改先寫進同資料夾的 `project.journal`，停手 1 秒或連續修改每 10 秒才重寫 project.json，所以 APP 執行中直接讀 project.json 可能落後最多 10 秒，要最新內容請用 `GET /api/projects/{pid}`。
- 介面開著時程式也能同時操作，畫面會即時更新；使用者按 Ctrl+Z 會收回程式送出的修改。
- `/program.html` 是第二螢幕用的節目畫面，由剪輯頁的播放器開啟，透過瀏覽器的 BroadcastChannel `vc-program` 跟著主視窗播放，不出聲，也沒有自己的 API。
- 時間都用秒（浮點數）。鏡頭的 `trimIn`／`trimOut` 是原始影片的時間，字幕的 `start`／`end` 是序列上的時間，兩者不同。
- 長工作（ingest 4K 檔、transcribe、export）可能要數分鐘到數十分鐘，請用 `GET /api/jobs/{jid}` 輪詢或聽 SSE 的 `job` 事件。
